"""Acceptance tests over the sample files in samples/scenarios.

Each sample CSV documents one scenario; these tests pin down what it must do,
so the samples handed to manual testers cannot silently drift from reality.
"""

from pathlib import Path

import pytest

from app.csv_parser import parse_hospital_csv
from app.directory_client import DirectoryClient, RetryPolicy
from app.jobs import JobStatus, JobStore, RowStatus
from app.processor import BulkProcessor
from tests.fake_directory import ScriptedDirectory

SCENARIOS = Path(__file__).resolve().parent.parent / "samples" / "scenarios"
NO_WAIT = RetryPolicy(max_attempts=3, base_delay=0, max_delay=0)


def parse(name):
    return parse_hospital_csv((SCENARIOS / name).read_bytes(), max_rows=20)


@pytest.mark.parametrize(
    "name, rows, warnings",
    [
        ("01-valid-20-hospitals-max.csv", 20, 0),
        ("02-valid-3-hospitals.csv", 3, 0),
        ("03-valid-1-hospital.csv", 1, 0),
        ("04-valid-no-phone-column.csv", 5, 0),
        ("05-valid-messy-header-with-bom.csv", 4, 0),
        ("06-valid-commas-and-quotes.csv", 3, 0),
        ("07-valid-accented-names.csv", 3, 0),
        ("08-valid-blank-lines-between-rows.csv", 4, 0),
        ("09-valid-windows-line-endings.csv", 4, 0),
        ("10-valid-extra-spaces-around-values.csv", 2, 0),
        ("11-warning-unknown-extra-column.csv", 3, 1),
        ("12-warning-duplicate-rows.csv", 5, 2),
    ],
)
def test_valid_samples(name, rows, warnings):
    result = parse(name)

    assert result.valid, result.errors
    assert len(result.rows) == rows
    assert len(result.warnings) == warnings


@pytest.mark.parametrize(
    "name, problem",
    [
        ("13-invalid-21-rows-over-limit.csv", "maximum is 20"),
        ("14-invalid-header-only.csv", "no hospital rows"),
        ("15-invalid-empty-file.csv", "empty"),
        ("16-invalid-missing-address-column.csv", "Missing required column(s): address"),
        ("17-invalid-blank-names.csv", "'name' is required"),
        ("18-invalid-blank-addresses.csv", "'address' is required"),
        ("19-invalid-too-many-fields-in-row.csv", "5 fields"),
        ("20-invalid-not-utf8-latin1.csv", "UTF-8"),
        ("21-invalid-many-problems-at-once.csv", "'name' is required"),
        ("22-invalid-semicolon-delimited.csv", "Missing required column(s): name, address"),
        ("23-invalid-json-renamed-to-csv.csv", "Missing required column(s)"),
    ],
)
def test_invalid_samples(name, problem):
    result = parse(name)

    assert not result.valid
    assert problem in result.errors[0].message
    assert result.rows == []


def test_many_problems_sample_reports_every_problem():
    errors = parse("21-invalid-many-problems-at-once.csv").errors

    assert [(e.row, e.field) for e in errors] == [(2, "name"), (3, "address"), (4, "address"), (5, None)]


def test_accented_sample_keeps_unicode_intact():
    names = [row.name for row in parse("07-valid-accented-names.csv").rows]

    assert names == ["Hôpital Saint-Louis", "Klinikum München Süd", "Clínica São José"]


# ---- Demo-mode samples: scripted failures through the real processor ---------

@pytest.fixture
def directory():
    return ScriptedDirectory(latency=0, attempts_per_run=NO_WAIT.max_attempts)


@pytest.fixture
async def processor(directory):
    async with directory.client() as http:
        yield BulkProcessor(DirectoryClient(http, retry=NO_WAIT), JobStore())


async def run_sample(processor, name):
    job = processor.submit(parse(name).rows)
    await processor.wait(job.batch_id)
    return job


async def resume(processor, job):
    processor.resume(job.batch_id)
    await processor.wait(job.batch_id)


async def test_d1_partial_failure_then_retry_completes(processor, directory):
    job = await run_sample(processor, "D1-partial-failure-then-retry.csv")

    assert job.status is JobStatus.PARTIALLY_FAILED
    failed = [r.row.name for r in job.rows if r.status is RowStatus.FAILED]
    assert failed == ["Bayside Psychiatric Hospital [server-error]", "Cedar Grove Clinic [rejected]"]
    assert not any(h["active"] for h in directory.batch(job.batch_id))

    await resume(processor, job)

    assert job.status is JobStatus.COMPLETED
    assert len(directory.batch(job.batch_id)) == job.total == 15
    assert all(h["active"] for h in directory.batch(job.batch_id))


async def test_d2_flaky_row_recovers_without_user_action(processor):
    job = await run_sample(processor, "D2-flaky-row-recovers-automatically.csv")

    assert job.status is JobStatus.COMPLETED
    assert job.rows[-1].attempts == 2


async def test_d3_timeout_after_save_creates_no_duplicate(processor, directory):
    job = await run_sample(processor, "D3-timeout-after-save-no-duplicate.csv")

    assert job.status is JobStatus.COMPLETED
    assert len(directory.batch(job.batch_id)) == job.total
    assert directory.count("POST /hospitals") == job.total


async def test_d4_activation_failure_then_retry(processor, directory):
    job = await run_sample(processor, "D4-activation-fails-then-retry.csv")

    assert job.status is JobStatus.ACTIVATION_FAILED
    assert job.processed == job.total

    await resume(processor, job)

    assert job.status is JobStatus.COMPLETED
    assert directory.count("POST /hospitals") == job.total


async def test_d5_failures_can_be_rolled_back(processor, directory):
    job = await run_sample(processor, "D5-everything-fails-roll-back.csv")
    assert (job.status, job.failed, job.processed) == (JobStatus.PARTIALLY_FAILED, 4, 3)

    deleted = await processor.rollback(job.batch_id)

    assert deleted == 3
    assert directory.batch(job.batch_id) == []


async def test_d6_twenty_rows_complete(processor, directory):
    job = await run_sample(processor, "D6-slow-20-rows-watch-progress.csv")

    assert job.status is JobStatus.COMPLETED
    assert len(directory.batch(job.batch_id)) == 20
