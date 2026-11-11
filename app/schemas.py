"""Public response models (these define the OpenAPI contract)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from app.csv_parser import CsvIssue, CsvParseResult
from app.jobs import BulkJob, JobStatus, RowStatus


class HospitalResult(BaseModel):
    row: int
    hospital_id: Optional[int]
    name: str
    status: RowStatus
    attempts: int
    error: Optional[str] = None


class JobSummary(BaseModel):
    batch_id: str
    status: JobStatus
    total_hospitals: int
    processed_hospitals: int
    failed_hospitals: int
    pending_hospitals: int
    processing_time_seconds: float
    batch_activated: bool
    resumable: bool
    error: Optional[str] = None
    warnings: List[str] = []
    created_at: datetime

    @classmethod
    def from_job(cls, job: BulkJob) -> "JobSummary":
        return cls(**_summary_fields(job))


class BulkJobResponse(JobSummary):
    hospitals: List[HospitalResult]

    @classmethod
    def from_job(cls, job: BulkJob) -> "BulkJobResponse":
        hospitals = [
            HospitalResult(
                row=r.row.row,
                hospital_id=r.hospital_id,
                name=r.row.name,
                status=r.status,
                attempts=r.attempts,
                error=r.error,
            )
            for r in job.rows
        ]
        return cls(**_summary_fields(job), hospitals=hospitals)


class RollbackResponse(BaseModel):
    deleted_count: int
    job: BulkJobResponse


class CsvIssueOut(BaseModel):
    row: Optional[int]
    field: Optional[str]
    message: str

    @classmethod
    def from_issue(cls, issue: CsvIssue) -> "CsvIssueOut":
        return cls(row=issue.row, field=issue.field, message=issue.message)


class HospitalPreview(BaseModel):
    row: int
    name: str
    address: str
    phone: Optional[str]


class ValidationReport(BaseModel):
    valid: bool
    total_hospitals: int
    errors: List[CsvIssueOut]
    warnings: List[CsvIssueOut]
    hospitals: List[HospitalPreview]

    @classmethod
    def from_result(cls, result: CsvParseResult) -> "ValidationReport":
        return cls(
            valid=result.valid,
            total_hospitals=len(result.rows),
            errors=[CsvIssueOut.from_issue(e) for e in result.errors],
            warnings=[CsvIssueOut.from_issue(w) for w in result.warnings],
            hospitals=[
                HospitalPreview(row=r.row, name=r.name, address=r.address, phone=r.phone)
                for r in result.rows
            ],
        )


class CsvRejected(BaseModel):
    detail: str
    errors: List[CsvIssueOut]
    warnings: List[CsvIssueOut]


def _summary_fields(job: BulkJob) -> dict:
    return dict(
        batch_id=job.batch_id,
        status=job.status,
        total_hospitals=job.total,
        processed_hospitals=job.processed,
        failed_hospitals=job.failed,
        pending_hospitals=job.pending,
        processing_time_seconds=job.processing_time_seconds,
        batch_activated=job.batch_activated,
        resumable=job.resumable,
        error=job.error,
        warnings=job.warnings,
        created_at=job.created_at,
    )
