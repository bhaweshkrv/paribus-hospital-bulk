"""Integration tests against the real Hospital Directory API.

Opt-in, because they write to a shared service and depend on the network:

    RUN_LIVE_TESTS=1 .venv/bin/pytest -m live

Every test deletes the batch it created, even when it fails.
"""

import os
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.csv_parser import HospitalRow
from app.directory_client import DirectoryClient, RetryPolicy
from app.main import create_app

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to run"),
]

UPSTREAM = os.environ.get("UPSTREAM_BASE_URL", "https://hospital-directory.onrender.com")
TIMEOUT = httpx.Timeout(90, connect=30)  # a sleeping free-tier dyno can take ~50 s to wake


@pytest.fixture
async def directory():
    async with httpx.AsyncClient(base_url=UPSTREAM, timeout=TIMEOUT) as http:
        yield DirectoryClient(http, retry=RetryPolicy(max_attempts=3, base_delay=1))


@pytest.fixture
async def batch_id(directory):
    batch_id = str(uuid.uuid4())
    yield batch_id
    await directory.delete_batch(batch_id)


async def test_upstream_contract_create_list_activate_delete(directory, batch_id):
    row = HospitalRow(row=1, name="Live Test Hospital", address="1 Integration Way", phone="555-0199")

    created = await directory.create_hospital(row, batch_id)
    listed = await directory.list_batch(batch_id)
    activated = await directory.activate_batch(batch_id)
    after = await directory.list_batch(batch_id)

    assert created["creation_batch_id"] == batch_id
    assert created["active"] is False
    assert [h["id"] for h in listed] == [created["id"]]
    assert activated == 1
    assert after[0]["active"] is True


async def test_upstream_unknown_batch_is_empty(directory):
    assert await directory.list_batch(str(uuid.uuid4())) == []


def test_bulk_upload_end_to_end_against_live_directory():
    rows = "".join(f"Live Bulk Hospital {i},{i} Integration Way,555-01{i:02d}\n" for i in range(1, 6))
    settings = Settings(upstream_base_url=UPSTREAM, read_timeout=90)
    with TestClient(create_app(settings)) as client:
        started = time.perf_counter()
        response = client.post(
            "/hospitals/bulk", files={"file": ("live.csv", "name,address,phone\n" + rows, "text/csv")}
        )
        elapsed = time.perf_counter() - started
        body = response.json()
        try:
            assert response.status_code == 200, body
            assert body["status"] == "completed"
            assert body["batch_activated"] is True
            assert {h["status"] for h in body["hospitals"]} == {"created_and_activated"}
            # Concurrent creates: 5 rows should take about as long as one (~6 s), not 5x.
            assert elapsed < 45, f"took {elapsed:.1f}s"
        finally:
            rollback = client.delete(f"/hospitals/bulk/{body['batch_id']}")
            assert rollback.json()["deleted_count"] == 5
