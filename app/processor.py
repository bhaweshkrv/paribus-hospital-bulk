"""Orchestrates bulk jobs against the Hospital Directory API.

Workflow for one job (also used, unchanged, for resumes):

1. Reconcile: if rows have been attempted before, adopt any hospitals that
   already exist upstream so a resume never duplicates them.
2. Create every unfinished row concurrently. The shared DirectoryClient caps
   in-flight requests across *all* jobs, so N simultaneous uploads cannot
   flood the upstream.
3. Activate the batch only if every row exists upstream. Otherwise the batch
   is left inactive (invisible to consumers) and the job is resumable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Iterable, Optional, Sequence

from app.csv_parser import HospitalRow
from app.directory_client import DirectoryClient, UpstreamError, UpstreamRejected
from app.jobs import BulkJob, JobStatus, JobStore, RowState, RowStatus

logger = logging.getLogger(__name__)


class JobNotFound(Exception):
    pass


class JobConflict(Exception):
    pass


class BulkProcessor:
    def __init__(self, client: DirectoryClient, store: JobStore) -> None:
        self.client = client
        self.store = store
        # Serialises "which upstream hospital belongs to which row" decisions.
        self._claim_lock = asyncio.Lock()
        self._tasks: Dict[str, asyncio.Task] = {}

    # -- public API ----------------------------------------------------------
    def submit(self, rows: Sequence[HospitalRow], warnings: Sequence[str] = ()) -> BulkJob:
        job = self.store.create(rows, warnings)
        self._start(job)
        return job

    def resume(self, batch_id: str) -> BulkJob:
        job = self.get(batch_id)
        if job.running:
            raise JobConflict("Job is still running.")
        if not job.resumable:
            raise JobConflict(f"Job is '{job.status.value}' and has nothing to resume.")
        self._start(job)
        return job

    async def rollback(self, batch_id: str) -> int:
        job = self.get(batch_id)
        if job.running:
            raise JobConflict("Job is still running.")
        if job.status is JobStatus.ROLLED_BACK:
            raise JobConflict("Job has already been rolled back.")
        deleted = await self.client.delete_batch(batch_id)
        for row in job.rows:
            if row.hospital_id is not None:
                row.status = RowStatus.ROLLED_BACK
        job.batch_activated = False
        job.status = JobStatus.ROLLED_BACK
        logger.info("batch=%s rolled back, %d hospital(s) deleted upstream", batch_id, deleted)
        return deleted

    def get(self, batch_id: str) -> BulkJob:
        job = self.store.get(batch_id)
        if job is None:
            raise JobNotFound(batch_id)
        return job

    async def wait(self, batch_id: str) -> BulkJob:
        task = self._tasks.get(batch_id)
        if task is not None:
            await asyncio.shield(task)
        return self.get(batch_id)

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # -- processing ----------------------------------------------------------
    def _start(self, job: BulkJob) -> None:
        job.start_run()
        task = asyncio.get_running_loop().create_task(self._run(job))
        self._tasks[job.batch_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.batch_id, None))

    async def _run(self, job: BulkJob) -> None:
        try:
            await self._process(job)
        except asyncio.CancelledError:
            job.finish_run(JobStatus.PARTIALLY_FAILED, "Processing was interrupted.")
            raise
        except Exception as exc:  # never leave a job stuck in a running state
            logger.exception("batch=%s crashed", job.batch_id)
            job.finish_run(JobStatus.PARTIALLY_FAILED, f"Unexpected error: {exc}")

    async def _process(self, job: BulkJob) -> None:
        todo = [r for r in job.rows if r.status in (RowStatus.PENDING, RowStatus.FAILED)]
        retried = [r for r in todo if r.attempts > 0]
        if retried:
            await self._adopt_landed(job, retried)
            todo = [r for r in todo if r.hospital_id is None]

        for row in todo:
            row.status, row.error = RowStatus.PENDING, None
        await asyncio.gather(*(self._create_row(job, row) for row in todo))

        if job.failed:
            job.finish_run(
                JobStatus.PARTIALLY_FAILED,
                f"{job.failed} of {job.total} hospital(s) failed; batch left inactive.",
            )
            logger.info("batch=%s partially failed (%d/%d)", job.batch_id, job.failed, job.total)
            return

        job.status = JobStatus.ACTIVATING
        try:
            activated = await self.client.activate_batch(job.batch_id)
        except UpstreamError as exc:
            job.finish_run(JobStatus.ACTIVATION_FAILED, f"Activation failed: {exc}")
            return

        if activated != job.total:
            job.warnings.append(
                f"Upstream activated {activated} hospital(s) but this batch created {job.total}."
            )
        for row in job.rows:
            row.status = RowStatus.CREATED_AND_ACTIVATED
        job.batch_activated = True
        job.finish_run(JobStatus.COMPLETED)
        logger.info(
            "batch=%s completed: %d hospital(s) in %.2fs",
            job.batch_id, job.total, job.processing_time_seconds,
        )

    async def _create_row(self, job: BulkJob, state: RowState) -> None:
        max_attempts = self.client.retry.max_attempts
        for attempt in range(1, max_attempts + 1):
            state.attempts += 1
            try:
                hospital = await self.client.create_hospital(state.row, job.batch_id)
            except UpstreamRejected as exc:
                state.mark_failed(str(exc))
                return
            except UpstreamError as exc:
                if attempt == max_attempts:
                    state.mark_failed(str(exc))
                    return
                await asyncio.sleep(self.client.retry.delay(attempt))
                # The failed attempt may still have been stored upstream.
                await self._adopt_landed(job, [state])
                if state.hospital_id is not None:
                    return
            else:
                async with self._claim_lock:
                    state.mark_created(hospital["id"])
                return

    async def _adopt_landed(self, job: BulkJob, states: Iterable[RowState]) -> None:
        """Match rows to hospitals already in the upstream batch, if any."""
        try:
            existing = await self.client.list_batch(job.batch_id)
        except UpstreamError:
            return  # can't tell; the caller will simply attempt the create
        async with self._claim_lock:
            claimed = {r.hospital_id for r in job.rows if r.hospital_id is not None}
            for state in states:
                match = _find_unclaimed(existing, state.row, claimed)
                if match is not None:
                    state.mark_created(match)
                    claimed.add(match)
                    logger.info(
                        "batch=%s row=%d adopted existing hospital %d",
                        job.batch_id, state.row.row, match,
                    )


def _find_unclaimed(hospitals: Iterable[dict], row: HospitalRow, claimed: set) -> Optional[int]:
    for hospital in hospitals:
        if (
            hospital["id"] not in claimed
            and hospital.get("name") == row.name
            and hospital.get("address") == row.address
            and hospital.get("phone") == row.phone
        ):
            return hospital["id"]
    return None
