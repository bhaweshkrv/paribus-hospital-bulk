import asyncio
import time

import pytest

from app.csv_parser import HospitalRow
from app.directory_client import DirectoryClient, RetryPolicy
from app.jobs import JobStatus, JobStore, RowStatus
from app.processor import BulkProcessor, JobConflict, JobNotFound
from tests.fake_directory import FakeDirectory

NO_WAIT = RetryPolicy(max_attempts=3, base_delay=0, max_delay=0)


def rows(n):
    return [HospitalRow(row=i, name=f"H{i}", address=f"{i} Main St") for i in range(1, n + 1)]


@pytest.fixture
def fake():
    return FakeDirectory()


@pytest.fixture
async def make_processor(fake):
    clients = []

    async def factory(max_concurrency=20, store=None):
        http = fake.client()
        clients.append(http)
        return BulkProcessor(
            DirectoryClient(http, retry=NO_WAIT, max_concurrency=max_concurrency),
            store or JobStore(),
        )

    yield factory
    for http in clients:
        await http.aclose()


async def run(processor, hospital_rows):
    job = processor.submit(hospital_rows)
    await processor.wait(job.batch_id)
    return job


async def test_creates_every_row_then_activates_the_batch(make_processor, fake):
    job = await run(await make_processor(), rows(3))

    assert job.status is JobStatus.COMPLETED
    assert job.batch_activated
    assert (job.total, job.processed, job.failed) == (3, 3, 0)
    assert [r.status for r in job.rows] == [RowStatus.CREATED_AND_ACTIVATED] * 3
    upstream = fake.batch(job.batch_id)
    assert sorted(h["id"] for h in upstream) == sorted(r.hospital_id for r in job.rows)
    assert all(h["active"] for h in upstream)


async def test_rows_are_created_concurrently(make_processor, fake):
    fake.latency = 0.2

    started = time.perf_counter()
    job = await run(await make_processor(), rows(20))
    elapsed = time.perf_counter() - started

    assert job.status is JobStatus.COMPLETED
    assert fake.max_in_flight == 20
    assert elapsed < 1.0  # sequential would take 20 * 0.2 = 4s


async def test_concurrency_limit_is_respected(make_processor, fake):
    fake.latency = 0.05

    await run(await make_processor(max_concurrency=4), rows(12))

    assert fake.max_in_flight == 4


async def test_transient_create_failure_is_retried(make_processor, fake):
    fake.fail_create("H2", "500", "timeout")

    job = await run(await make_processor(), rows(3))

    assert job.status is JobStatus.COMPLETED
    assert job.rows[1].attempts == 3
    assert len(fake.batch(job.batch_id)) == 3


async def test_timeout_after_upstream_commit_does_not_create_a_duplicate(make_processor, fake):
    fake.fail_create("H2", "commit_timeout")

    job = await run(await make_processor(), rows(3))

    assert job.status is JobStatus.COMPLETED
    assert len(fake.batch(job.batch_id)) == 3
    assert fake.count("POST /hospitals") == 3
    assert job.rows[1].hospital_id in {h["id"] for h in fake.batch(job.batch_id)}


async def test_rejected_row_fails_without_retry_and_batch_stays_inactive(make_processor, fake):
    fake.fail_create("H2", "422")

    job = await run(await make_processor(), rows(3))

    assert job.status is JobStatus.PARTIALLY_FAILED
    assert not job.batch_activated
    assert (job.processed, job.failed) == (2, 1)
    assert job.rows[1].status is RowStatus.FAILED
    assert job.rows[1].attempts == 1
    assert "rejected by upstream" in job.rows[1].error
    assert fake.count("PATCH") == 0
    assert not any(h["active"] for h in fake.batch(job.batch_id))


async def test_row_fails_after_exhausting_retries(make_processor, fake):
    fake.fail_create("H1", "500", "500", "500")

    job = await run(await make_processor(), rows(2))

    assert job.status is JobStatus.PARTIALLY_FAILED
    assert job.rows[0].attempts == 3
    assert job.rows[0].hospital_id is None


async def test_activation_failure_is_reported_and_resumable(make_processor, fake):
    processor = await make_processor()
    fake.fail_activate("500", "500", "500")

    job = await run(processor, rows(2))

    assert job.status is JobStatus.ACTIVATION_FAILED
    assert job.resumable
    assert [r.status for r in job.rows] == [RowStatus.CREATED] * 2

    processor.resume(job.batch_id)
    await processor.wait(job.batch_id)

    assert job.status is JobStatus.COMPLETED
    assert fake.count("POST /hospitals") == 2  # nothing re-created


async def test_resume_retries_only_failed_rows_in_the_same_batch(make_processor, fake):
    processor = await make_processor()
    fake.fail_create("H2", "500", "500", "500")
    job = await run(processor, rows(3))
    assert job.status is JobStatus.PARTIALLY_FAILED

    resumed = processor.resume(job.batch_id)
    await processor.wait(job.batch_id)

    assert resumed is job
    assert job.status is JobStatus.COMPLETED
    assert job.batch_activated
    assert fake.count("POST /hospitals") == 3 + 3  # 3 initial creates + 2 retries, then 1 on resume
    assert len(fake.batch(job.batch_id)) == 3


async def test_resume_adopts_a_row_whose_last_attempt_actually_landed(make_processor, fake):
    processor = await make_processor()
    # The final attempt is stored upstream even though we saw a timeout.
    fake.fail_create("H1", "500", "500", "commit_timeout")
    job = await run(processor, rows(1))
    assert job.status is JobStatus.PARTIALLY_FAILED

    processor.resume(job.batch_id)
    await processor.wait(job.batch_id)

    assert job.status is JobStatus.COMPLETED
    assert len(fake.batch(job.batch_id)) == 1
    assert fake.count("POST /hospitals") == 3


async def test_resume_rejects_completed_and_unknown_jobs(make_processor):
    processor = await make_processor()
    job = await run(processor, rows(1))

    with pytest.raises(JobConflict):
        processor.resume(job.batch_id)
    with pytest.raises(JobNotFound):
        processor.resume("00000000-0000-0000-0000-000000000000")


async def test_resume_rejects_a_job_that_is_still_running(make_processor, fake):
    fake.latency = 0.1
    processor = await make_processor()
    job = processor.submit(rows(2))
    await asyncio.sleep(0.02)

    with pytest.raises(JobConflict):
        processor.resume(job.batch_id)
    await processor.wait(job.batch_id)


async def test_progress_is_observable_while_processing(make_processor, fake):
    fake.latency = 0.1
    fake.fail_create("H2", "500")  # H2 needs a second round trip
    processor = await make_processor()

    job = processor.submit(rows(2))
    await asyncio.sleep(0.15)

    assert job.status is JobStatus.PROCESSING
    assert (job.processed, job.pending) == (1, 1)
    assert job.processing_time_seconds > 0
    await processor.wait(job.batch_id)
    assert job.status is JobStatus.COMPLETED


async def test_rollback_deletes_the_upstream_batch(make_processor, fake):
    processor = await make_processor()
    fake.fail_create("H2", "422")
    job = await run(processor, rows(3))

    deleted = await processor.rollback(job.batch_id)

    assert deleted == 2
    assert job.status is JobStatus.ROLLED_BACK
    assert fake.batch(job.batch_id) == []
    assert not job.resumable


def test_job_store_evicts_oldest_finished_jobs_beyond_capacity():
    store = JobStore(max_jobs=2)
    jobs = [store.create(rows(1)) for _ in range(3)]
    jobs[0].status = JobStatus.COMPLETED

    store.evict()

    assert store.get(jobs[0].batch_id) is None
    assert store.get(jobs[1].batch_id) is jobs[1]  # still pending, never evicted
    assert store.get(jobs[2].batch_id) is jobs[2]


async def test_shutdown_marks_running_jobs_interrupted_and_resumable(make_processor, fake):
    fake.latency = 1.0
    processor = await make_processor()
    job = processor.submit(rows(2))
    await asyncio.sleep(0.05)

    await processor.shutdown()

    assert job.status is JobStatus.PARTIALLY_FAILED
    assert job.error == "Processing was interrupted."
    assert job.resumable


async def test_unexpected_error_never_leaves_a_job_stuck_running(make_processor, monkeypatch):
    processor = await make_processor()

    async def explode(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(processor.client, "create_hospital", explode)
    job = await run(processor, rows(1))

    assert job.status is JobStatus.PARTIALLY_FAILED
    assert job.error == "Unexpected error: disk on fire"
    assert not job.running


async def test_activation_count_mismatch_is_reported_as_a_warning(make_processor, fake):
    processor = await make_processor()
    job = processor.submit(rows(2))
    # Someone else adds a hospital to the same batch while we work.
    fake.hospitals[999] = {"id": 999, "name": "Intruder", "address": "x", "phone": None,
                           "creation_batch_id": job.batch_id, "active": False, "created_at": ""}
    await processor.wait(job.batch_id)

    assert job.status is JobStatus.COMPLETED
    assert job.warnings == ["Upstream activated 3 hospital(s) but this batch created 2."]


async def test_retry_still_happens_when_the_duplicate_check_itself_fails(make_processor, fake, monkeypatch):
    processor = await make_processor()
    fake.fail_create("H1", "500")

    async def directory_down(batch_id):
        from app.directory_client import UpstreamUnavailable
        raise UpstreamUnavailable("down")

    monkeypatch.setattr(processor.client, "list_batch", directory_down)
    job = await run(processor, rows(1))

    assert job.status is JobStatus.COMPLETED
    assert job.rows[0].attempts == 2


async def test_rollback_rejects_running_already_rolled_back_and_unknown_jobs(make_processor, fake):
    fake.latency = 0.1
    processor = await make_processor()
    job = processor.submit(rows(1))

    with pytest.raises(JobConflict):
        await processor.rollback(job.batch_id)
    await processor.wait(job.batch_id)
    await processor.rollback(job.batch_id)
    with pytest.raises(JobConflict):
        await processor.rollback(job.batch_id)
    with pytest.raises(JobNotFound):
        await processor.rollback("00000000-0000-0000-0000-000000000000")


async def test_rolled_back_rows_are_marked_and_batch_is_no_longer_live(make_processor):
    processor = await make_processor()
    job = await run(processor, rows(2))
    assert job.batch_activated

    await processor.rollback(job.batch_id)

    assert not job.batch_activated
    assert [r.status for r in job.rows] == [RowStatus.ROLLED_BACK] * 2


async def test_concurrent_jobs_share_one_global_concurrency_limit(make_processor, fake):
    fake.latency = 0.05
    processor = await make_processor(max_concurrency=6)

    jobs = [processor.submit(rows(10)) for _ in range(5)]
    await asyncio.gather(*(processor.wait(j.batch_id) for j in jobs))

    assert all(j.status is JobStatus.COMPLETED for j in jobs)
    assert fake.max_in_flight == 6
    assert len(fake.hospitals) == 50


async def test_waiting_on_a_finished_job_returns_immediately(make_processor):
    processor = await make_processor()
    job = await run(processor, rows(1))

    assert await processor.wait(job.batch_id) is job
