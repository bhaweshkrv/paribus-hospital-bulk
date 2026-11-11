"""In-memory state for bulk jobs.

A job is keyed by its upstream batch ID, so the ID a client polls with is the
same one it can use against the Hospital Directory API directly.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Sequence

from app.csv_parser import HospitalRow


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    ACTIVATING = "activating"
    COMPLETED = "completed"
    PARTIALLY_FAILED = "partially_failed"
    ACTIVATION_FAILED = "activation_failed"
    ROLLED_BACK = "rolled_back"


RUNNING_STATUSES = {JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.ACTIVATING}
RESUMABLE_STATUSES = {JobStatus.PARTIALLY_FAILED, JobStatus.ACTIVATION_FAILED}


class RowStatus(str, Enum):
    PENDING = "pending"
    CREATED = "created"
    CREATED_AND_ACTIVATED = "created_and_activated"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class RowState:
    row: HospitalRow
    status: RowStatus = RowStatus.PENDING
    hospital_id: Optional[int] = None
    error: Optional[str] = None
    attempts: int = 0

    def mark_created(self, hospital_id: int) -> None:
        self.status = RowStatus.CREATED
        self.hospital_id = hospital_id
        self.error = None

    def mark_failed(self, error: str) -> None:
        self.status = RowStatus.FAILED
        self.error = error


@dataclass
class BulkJob:
    batch_id: str
    rows: List[RowState]
    warnings: List[str] = field(default_factory=list)
    status: JobStatus = JobStatus.PENDING
    batch_activated: bool = False
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _elapsed: float = 0.0
    _run_started: Optional[float] = None

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def processed(self) -> int:
        """Rows that exist upstream (created, whether or not activated yet)."""
        return sum(
            1 for r in self.rows if r.status in (RowStatus.CREATED, RowStatus.CREATED_AND_ACTIVATED)
        )

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r.status is RowStatus.FAILED)

    @property
    def pending(self) -> int:
        return sum(1 for r in self.rows if r.status is RowStatus.PENDING)

    @property
    def running(self) -> bool:
        return self.status in RUNNING_STATUSES

    @property
    def resumable(self) -> bool:
        return self.status in RESUMABLE_STATUSES

    @property
    def processing_time_seconds(self) -> float:
        """Wall time spent processing, summed across the initial run and resumes."""
        elapsed = self._elapsed
        if self._run_started is not None:
            elapsed += time.perf_counter() - self._run_started
        return round(elapsed, 3)

    def start_run(self) -> None:
        self.status = JobStatus.PROCESSING
        self.error = None
        self._run_started = time.perf_counter()

    def finish_run(self, status: JobStatus, error: Optional[str] = None) -> None:
        if self._run_started is not None:
            self._elapsed += time.perf_counter() - self._run_started
            self._run_started = None
        self.status = status
        self.error = error


class JobStore:
    """Bounded in-memory job registry.

    Finished jobs beyond `max_jobs` are evicted oldest-first so memory stays
    flat under sustained load; running jobs are never evicted.
    """

    def __init__(self, max_jobs: int = 500) -> None:
        self.max_jobs = max_jobs
        self._jobs: "OrderedDict[str, BulkJob]" = OrderedDict()

    def create(self, rows: Sequence[HospitalRow], warnings: Sequence[str] = ()) -> BulkJob:
        job = BulkJob(
            batch_id=str(uuid.uuid4()),
            rows=[RowState(row) for row in rows],
            warnings=list(warnings),
        )
        self._jobs[job.batch_id] = job
        self.evict()
        return job

    def get(self, batch_id: str) -> Optional[BulkJob]:
        return self._jobs.get(batch_id)

    def list(self) -> List[BulkJob]:
        return list(reversed(self._jobs.values()))

    def evict(self) -> None:
        overflow = len(self._jobs) - self.max_jobs
        if overflow <= 0:
            return
        finished = [batch_id for batch_id, job in self._jobs.items() if not job.running]
        for batch_id in finished[:overflow]:
            del self._jobs[batch_id]

    def __len__(self) -> int:
        return len(self._jobs)
