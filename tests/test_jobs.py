import time

from app.csv_parser import HospitalRow
from app.jobs import JobStatus, JobStore, RowStatus


def rows(n):
    return [HospitalRow(row=i, name=f"H{i}", address=f"{i} Main St") for i in range(1, n + 1)]


def test_new_job_starts_pending_with_every_row_pending():
    job = JobStore().create(rows(3), warnings=["Row 2: duplicate"])

    assert job.status is JobStatus.PENDING
    assert (job.total, job.processed, job.failed, job.pending) == (3, 0, 0, 3)
    assert job.warnings == ["Row 2: duplicate"]
    assert job.running and not job.resumable


def test_counters_follow_row_states():
    job = JobStore().create(rows(4))
    job.rows[0].mark_created(10)
    job.rows[1].status = RowStatus.CREATED_AND_ACTIVATED
    job.rows[2].mark_failed("boom")

    assert (job.processed, job.failed, job.pending) == (2, 1, 1)
    assert job.rows[2].error == "boom"


def test_marking_created_clears_a_previous_error():
    job = JobStore().create(rows(1))
    job.rows[0].mark_failed("timeout")

    job.rows[0].mark_created(7)

    assert (job.rows[0].status, job.rows[0].hospital_id, job.rows[0].error) == (RowStatus.CREATED, 7, None)


def test_processing_time_accumulates_across_runs_and_freezes_between_them():
    job = JobStore().create(rows(1))

    job.start_run()
    time.sleep(0.02)
    job.finish_run(JobStatus.PARTIALLY_FAILED, "1 failed")
    first = job.processing_time_seconds
    time.sleep(0.02)
    assert job.processing_time_seconds == first  # idle time is not counted

    job.start_run()
    time.sleep(0.02)
    job.finish_run(JobStatus.COMPLETED)

    assert first >= 0.02
    assert job.processing_time_seconds >= first + 0.02
    assert job.error is None


def test_only_failed_states_are_resumable():
    job = JobStore().create(rows(1))
    expectations = {
        JobStatus.PENDING: False,
        JobStatus.PROCESSING: False,
        JobStatus.ACTIVATING: False,
        JobStatus.COMPLETED: False,
        JobStatus.PARTIALLY_FAILED: True,
        JobStatus.ACTIVATION_FAILED: True,
        JobStatus.ROLLED_BACK: False,
    }
    for status, resumable in expectations.items():
        job.status = status
        assert job.resumable is resumable, status


def test_store_lists_newest_first_and_counts_jobs():
    store = JobStore()
    first, second = store.create(rows(1)), store.create(rows(1))

    assert store.list() == [second, first]
    assert len(store) == 2
    assert store.get("missing") is None


def test_store_never_evicts_running_jobs_even_when_over_capacity():
    store = JobStore(max_jobs=1)
    jobs = [store.create(rows(1)) for _ in range(3)]  # all still pending

    assert len(store) == 3
    jobs[0].status = jobs[1].status = JobStatus.COMPLETED
    store.evict()

    assert store.list() == [jobs[2]]
