from __future__ import annotations

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from app.config import Settings
from app.csv_parser import CsvParseResult, parse_hospital_csv
from app.directory_client import UpstreamError
from app.jobs import BulkJob
from app.processor import BulkProcessor, JobConflict, JobNotFound
from app.schemas import (
    BulkJobResponse,
    CsvIssueOut,
    CsvRejected,
    JobSummary,
    RollbackResponse,
    ValidationReport,
)

router = APIRouter(prefix="/hospitals/bulk", tags=["bulk"])

WAIT_QUERY = Query(
    True,
    description="Wait for processing to finish (200). "
    "With `false`, return 202 immediately and poll `GET /hospitals/bulk/{batch_id}`.",
)


def get_processor(request: Request) -> BulkProcessor:
    return request.app.state.processor


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def read_csv(file: UploadFile, settings: Settings) -> CsvParseResult:
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, f"File is too large. The limit is {_human_size(settings.max_upload_bytes)}.")
    return parse_hospital_csv(content, max_rows=settings.max_csv_rows)


@router.post(
    "",
    response_model=BulkJobResponse,
    responses={202: {"model": BulkJobResponse}, 413: {}, 422: {"model": CsvRejected}},
    summary="Bulk create hospitals from a CSV file",
)
async def bulk_create(
    file: UploadFile = File(..., description="CSV with header name,address,phone (phone optional)"),
    wait: bool = WAIT_QUERY,
    processor: BulkProcessor = Depends(get_processor),
    settings: Settings = Depends(get_settings),
):
    result = await read_csv(file, settings)
    if not result.valid:
        body = CsvRejected(
            detail="CSV validation failed; nothing was created.",
            errors=[CsvIssueOut.from_issue(e) for e in result.errors],
            warnings=[CsvIssueOut.from_issue(w) for w in result.warnings],
        )
        return JSONResponse(body.model_dump(), status_code=422)

    warnings = [f"Row {w.row}: {w.message}" if w.row else w.message for w in result.warnings]
    job = processor.submit(result.rows, warnings)
    return await _respond(processor, job, wait)


@router.post("/validate", response_model=ValidationReport, summary="Validate a CSV without processing it")
async def validate_csv(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
):
    return ValidationReport.from_result(await read_csv(file, settings))


@router.get("", response_model=List[JobSummary], summary="List bulk jobs, newest first")
async def list_jobs(processor: BulkProcessor = Depends(get_processor)):
    return [JobSummary.from_job(job) for job in processor.store.list()]


@router.get("/{batch_id}", response_model=BulkJobResponse, summary="Job status and per-row progress")
async def get_job(batch_id: UUID, processor: BulkProcessor = Depends(get_processor)):
    return BulkJobResponse.from_job(_get(processor, batch_id))


@router.post(
    "/{batch_id}/resume",
    response_model=BulkJobResponse,
    responses={202: {"model": BulkJobResponse}, 404: {}, 409: {}},
    summary="Retry failed rows (or a failed activation) in the same batch",
)
async def resume_job(
    batch_id: UUID, wait: bool = WAIT_QUERY, processor: BulkProcessor = Depends(get_processor)
):
    try:
        job = processor.resume(str(batch_id))
    except JobNotFound:
        raise HTTPException(404, "Batch not found.")
    except JobConflict as exc:
        raise HTTPException(409, str(exc))
    return await _respond(processor, job, wait)


@router.delete(
    "/{batch_id}",
    response_model=RollbackResponse,
    responses={404: {}, 409: {}, 502: {}},
    summary="Roll back a batch: delete its hospitals from the directory",
)
async def rollback_job(batch_id: UUID, processor: BulkProcessor = Depends(get_processor)):
    try:
        deleted = await processor.rollback(str(batch_id))
    except JobNotFound:
        raise HTTPException(404, "Batch not found.")
    except JobConflict as exc:
        raise HTTPException(409, str(exc))
    except UpstreamError as exc:
        raise HTTPException(502, f"Directory API error: {exc}")
    job = processor.get(str(batch_id))
    return RollbackResponse(deleted_count=deleted, job=BulkJobResponse.from_job(job))


def _human_size(size: int) -> str:
    return f"{size / 1_048_576:g} MB" if size >= 1_048_576 else f"{size / 1024:g} KB"


def _get(processor: BulkProcessor, batch_id: UUID) -> BulkJob:
    try:
        return processor.get(str(batch_id))
    except JobNotFound:
        raise HTTPException(404, "Batch not found.")


async def _respond(processor: BulkProcessor, job: BulkJob, wait: bool):
    if not wait:
        return JSONResponse(
            BulkJobResponse.from_job(job).model_dump(mode="json"),
            status_code=202,
            headers={"Location": f"/hospitals/bulk/{job.batch_id}"},
        )
    await processor.wait(job.batch_id)
    return BulkJobResponse.from_job(job)
