import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fake_directory import BASE_URL, FakeDirectory

VALID_CSV = b"name,address,phone\nGeneral Hospital,1 Main St,555-0001\nCity Clinic,2 Oak Ave,\n"


@pytest.fixture
def fake():
    return FakeDirectory()


@pytest.fixture
def client(fake):
    settings = Settings(
        upstream_base_url=BASE_URL,
        retry_base_delay=0,
        retry_max_delay=0,
        warm_upstream_on_startup=False,
        max_upload_bytes=4096,
    )
    with TestClient(create_app(settings, transport=fake.transport())) as test_client:
        yield test_client


def upload(client, content=VALID_CSV, **params):
    return client.post(
        "/hospitals/bulk", params=params, files={"file": ("hospitals.csv", content, "text/csv")}
    )


def poll_until_finished(client, batch_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/hospitals/bulk/{batch_id}").json()
        if body["status"] not in ("pending", "processing", "activating"):
            return body
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def test_bulk_create_returns_the_documented_response(client, fake):
    response = upload(client)

    assert response.status_code == 200
    body = response.json()
    assert body["total_hospitals"] == 2
    assert body["processed_hospitals"] == 2
    assert body["failed_hospitals"] == 0
    assert body["batch_activated"] is True
    assert isinstance(body["processing_time_seconds"], float)
    assert body["hospitals"][0] == {
        "row": 1,
        "hospital_id": 1,
        "name": "General Hospital",
        "status": "created_and_activated",
        "attempts": 1,
        "error": None,
    }
    assert len(fake.batch(body["batch_id"])) == 2


def test_invalid_csv_is_rejected_before_any_upstream_call(client, fake):
    response = upload(client, b"name,address\n,1 Main St\n")

    assert response.status_code == 422
    body = response.json()
    assert body["errors"] == [{"row": 1, "field": "name", "message": "'name' is required."}]
    assert fake.requests == []


def test_oversized_upload_is_rejected(client, fake):
    response = upload(client, b"name,address\n" + b"x" * 5000)

    assert response.status_code == 413
    assert response.json()["detail"] == "File is too large. The limit is 4 KB."
    assert fake.requests == []


def test_missing_file_field_is_a_validation_error(client):
    assert client.post("/hospitals/bulk").status_code == 422


def test_async_mode_returns_202_and_progress_can_be_polled(client, fake):
    fake.latency = 0.05

    response = upload(client, wait="false")

    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    assert response.headers["location"] == f"/hospitals/bulk/{batch_id}"
    assert response.json()["status"] == "processing"
    final = poll_until_finished(client, batch_id)
    assert final["status"] == "completed"
    assert final["batch_activated"] is True


def test_partial_failure_can_be_resumed(client, fake):
    fake.fail_create("City Clinic", "500", "500", "500")

    first = upload(client).json()
    assert first["status"] == "partially_failed"
    assert first["batch_activated"] is False
    assert first["resumable"] is True
    assert first["hospitals"][1]["status"] == "failed"

    resumed = client.post(f"/hospitals/bulk/{first['batch_id']}/resume")

    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["batch_activated"] is True


def test_resuming_a_completed_job_is_a_conflict(client):
    batch_id = upload(client).json()["batch_id"]

    assert client.post(f"/hospitals/bulk/{batch_id}/resume").status_code == 409


def test_rollback_deletes_the_batch_upstream(client, fake):
    fake.fail_create("City Clinic", "422")
    batch_id = upload(client).json()["batch_id"]

    response = client.delete(f"/hospitals/bulk/{batch_id}")

    assert response.status_code == 200
    assert response.json()["deleted_count"] == 1
    assert response.json()["job"]["status"] == "rolled_back"
    assert fake.batch(batch_id) == []


def test_unknown_and_malformed_batch_ids(client):
    assert client.get("/hospitals/bulk/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/hospitals/bulk/not-a-uuid").status_code == 422


def test_jobs_are_listed_newest_first_without_row_details(client):
    first = upload(client).json()["batch_id"]
    second = upload(client).json()["batch_id"]

    jobs = client.get("/hospitals/bulk").json()

    assert [j["batch_id"] for j in jobs] == [second, first]
    assert "hospitals" not in jobs[0]


def test_validate_endpoint_reports_without_creating_anything(client, fake):
    valid = client.post("/hospitals/bulk/validate", files={"file": ("h.csv", VALID_CSV)}).json()
    invalid = client.post(
        "/hospitals/bulk/validate", files={"file": ("h.csv", b"name\nGeneral\n")}
    ).json()

    assert valid["valid"] is True
    assert valid["total_hospitals"] == 2
    assert valid["hospitals"][1] == {"row": 2, "name": "City Clinic", "address": "2 Oak Ave", "phone": None}
    assert invalid["valid"] is False
    assert "address" in invalid["errors"][0]["message"]
    assert fake.requests == []


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_upstream_health_reports_reachability_and_latency(client):
    body = client.get("/health/upstream").json()

    assert body["reachable"] is True
    assert body["latency_ms"] >= 0


def test_upstream_health_reports_unreachable_upstream(fake):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    settings = Settings(upstream_base_url=BASE_URL, warm_upstream_on_startup=False)
    with TestClient(create_app(settings, transport=httpx.MockTransport(refuse))) as c:
        assert c.get("/health/upstream").json()["reachable"] is False


def test_root_serves_the_web_ui(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Hospital bulk import" in response.text


def test_ui_assets_are_served_locally(client):
    font = client.get("/static/fonts/public-sans-400-700.woff2")
    icons = client.get("/static/icons/phosphor.css")

    assert font.status_code == 200
    assert font.headers["content-type"] == "font/woff2"
    assert icons.status_code == 200
    assert client.get("/static/favicon.svg").headers["content-type"] == "image/svg+xml"


def test_resume_unknown_batch_is_404(client):
    assert client.post("/hospitals/bulk/00000000-0000-0000-0000-000000000000/resume").status_code == 404


def test_resume_in_async_mode_returns_202_and_finishes(client, fake):
    fake.fail_create("City Clinic", "500", "500", "500")
    batch_id = upload(client).json()["batch_id"]

    response = client.post(f"/hospitals/bulk/{batch_id}/resume", params={"wait": "false"})

    assert response.status_code == 202
    assert response.headers["location"] == f"/hospitals/bulk/{batch_id}"
    assert poll_until_finished(client, batch_id)["status"] == "completed"


def test_rollback_unknown_batch_is_404(client):
    assert client.delete("/hospitals/bulk/00000000-0000-0000-0000-000000000000").status_code == 404


def test_rollback_while_running_is_409(client, fake):
    fake.latency = 0.3
    batch_id = upload(client, wait="false").json()["batch_id"]

    response = client.delete(f"/hospitals/bulk/{batch_id}")

    assert response.status_code == 409
    poll_until_finished(client, batch_id)


def test_rollback_reports_directory_failure_as_502(client, fake):
    batch_id = upload(client).json()["batch_id"]
    fake.transport_down = True

    response = client.delete(f"/hospitals/bulk/{batch_id}")

    assert response.status_code == 502
    assert "Hospital Directory" in response.json()["detail"]
    assert client.get(f"/hospitals/bulk/{batch_id}").json()["status"] == "completed"


def test_validate_rejects_oversized_upload(client):
    response = client.post("/hospitals/bulk/validate", files={"file": ("h.csv", b"x" * 5000)})

    assert response.status_code == 413


def test_csv_warnings_are_carried_onto_the_job(client):
    body = upload(client, b"name,address,website\nGeneral,1 Main St,x\n").json()

    assert body["warnings"] == ["Ignoring unknown column(s): website."]


def test_startup_wakes_the_upstream_when_enabled(fake):
    settings = Settings(upstream_base_url=BASE_URL)
    with TestClient(create_app(settings, transport=fake.transport())) as c:
        poll = time.monotonic() + 2
        while "GET /" not in fake.requests and time.monotonic() < poll:
            time.sleep(0.01)
        c.get("/health")

    assert "GET /" in fake.requests


def test_openapi_documents_every_bulk_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) >= {
        "/hospitals/bulk",
        "/hospitals/bulk/validate",
        "/hospitals/bulk/{batch_id}",
        "/hospitals/bulk/{batch_id}/resume",
        "/health",
        "/health/upstream",
    }
    assert client.get("/docs").status_code == 200
