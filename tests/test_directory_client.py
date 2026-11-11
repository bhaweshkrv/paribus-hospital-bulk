import httpx
import pytest

from app.csv_parser import HospitalRow
from app.directory_client import (
    DirectoryClient,
    RetryPolicy,
    UpstreamRejected,
    UpstreamUnavailable,
)
from tests.fake_directory import FakeDirectory, new_batch_id

NO_WAIT = RetryPolicy(max_attempts=3, base_delay=0, max_delay=0)
ROW = HospitalRow(row=1, name="General", address="1 Main St", phone="555")


@pytest.fixture
def fake():
    return FakeDirectory()


@pytest.fixture
async def client(fake):
    async with fake.client() as http:
        yield DirectoryClient(http, retry=NO_WAIT)


async def test_create_sends_row_with_batch_id_and_returns_hospital(client, fake):
    batch_id = new_batch_id()

    hospital = await client.create_hospital(ROW, batch_id)

    assert hospital["id"] == 1
    stored = fake.hospitals[1]
    assert (stored["name"], stored["address"], stored["phone"]) == ("General", "1 Main St", "555")
    assert stored["creation_batch_id"] == batch_id
    assert stored["active"] is False


async def test_create_validation_error_is_a_non_retryable_rejection(client, fake):
    fake.fail_create("General", "422")

    with pytest.raises(UpstreamRejected) as exc:
        await client.create_hospital(ROW, new_batch_id())

    assert "rejected by upstream" in str(exc.value)


@pytest.mark.parametrize("outcome", ["500", "timeout"])
async def test_create_does_not_retry_by_itself(client, fake, outcome):
    # POST is not idempotent: blind retries could duplicate hospitals, so the
    # client surfaces the failure and leaves recovery to the processor.
    fake.fail_create("General", outcome)

    with pytest.raises(UpstreamUnavailable):
        await client.create_hospital(ROW, new_batch_id())

    assert fake.count("POST /hospitals") == 1


async def test_list_batch_returns_empty_list_for_unknown_batch(client):
    assert await client.list_batch(new_batch_id()) == []


async def test_activate_returns_activated_count(client, fake):
    batch_id = new_batch_id()
    await client.create_hospital(ROW, batch_id)

    assert await client.activate_batch(batch_id) == 1
    assert fake.hospitals[1]["active"] is True


async def test_activate_retries_transient_failures(client, fake):
    batch_id = new_batch_id()
    await client.create_hospital(ROW, batch_id)
    fake.fail_activate("503", "timeout")

    assert await client.activate_batch(batch_id) == 1
    assert fake.count("PATCH") == 3


async def test_activate_gives_up_after_max_attempts(client, fake):
    fake.fail_activate("500", "500", "500")

    with pytest.raises(UpstreamUnavailable):
        await client.activate_batch(new_batch_id())

    assert fake.count("PATCH") == 3


async def test_delete_batch_returns_deleted_count_and_zero_when_missing(client, fake):
    batch_id = new_batch_id()
    await client.create_hospital(ROW, batch_id)

    assert await client.delete_batch(batch_id) == 1
    assert await client.delete_batch(batch_id) == 0


def test_retry_delay_grows_exponentially_and_is_capped():
    policy = RetryPolicy(max_attempts=5, base_delay=1, max_delay=3, jitter=False)

    assert [policy.delay(n) for n in (1, 2, 3, 4)] == [1, 2, 3, 3]


async def test_connection_errors_map_to_upstream_unavailable(fake):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(refuse)) as http:
        client = DirectoryClient(http, retry=NO_WAIT)
        with pytest.raises(UpstreamUnavailable):
            await client.list_batch(new_batch_id())


@pytest.mark.parametrize(
    "outcome, expected",
    [
        ("422", "The Hospital Directory rejected this request: rejected by upstream (HTTP 422)."),
        ("500", "The Hospital Directory returned an error (HTTP 500)."),
        ("timeout", "The Hospital Directory didn't respond in time."),
    ],
)
async def test_errors_are_described_in_plain_language(client, fake, outcome, expected):
    fake.fail_create("General", outcome)

    with pytest.raises((UpstreamRejected, UpstreamUnavailable)) as exc:
        await client.create_hospital(ROW, new_batch_id())

    assert str(exc.value) == expected


def _client_for(handler):
    http = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler))
    return http, DirectoryClient(http, retry=NO_WAIT)


@pytest.mark.parametrize("method", ["list_batch", "delete_batch"])
async def test_rejections_other_than_404_are_raised(method):
    http, client = _client_for(lambda request: httpx.Response(403, json={"detail": "forbidden"}))
    async with http:
        with pytest.raises(UpstreamRejected) as exc:
            await getattr(client, method)(new_batch_id())

    assert exc.value.status_code == 403
    assert "forbidden" in str(exc.value)


async def test_rejection_with_a_non_json_body_uses_the_raw_text():
    http, client = _client_for(lambda request: httpx.Response(400, text="Bad request, plain text"))
    async with http:
        with pytest.raises(UpstreamRejected) as exc:
            await client.activate_batch(new_batch_id())

    assert "Bad request, plain text" in str(exc.value)


async def test_rate_limiting_is_treated_as_transient_and_retried():
    responses = iter([httpx.Response(429), httpx.Response(200, json={"activated_count": 2})])
    http, client = _client_for(lambda request: next(responses))
    async with http:
        assert await client.activate_batch(new_batch_id()) == 2


async def test_ping_treats_any_http_response_as_reachable():
    http, client = _client_for(lambda request: httpx.Response(404))
    async with http:
        assert await client.ping() is True


async def test_ping_reports_unreachable_on_transport_failure():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    http, client = _client_for(refuse)
    async with http:
        assert await client.ping() is False


def test_jittered_delay_stays_between_half_and_full_backoff():
    policy = RetryPolicy(base_delay=2, max_delay=100, jitter=True)

    delays = [policy.delay(2) for _ in range(200)]

    assert all(2 <= d <= 4 for d in delays)
    assert len(set(delays)) > 1
