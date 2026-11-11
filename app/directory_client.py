"""Async client for the upstream Hospital Directory API.

Retry semantics follow HTTP idempotency:

* GET / PATCH / DELETE are idempotent, so they are retried here with
  exponential backoff on transient failures (timeouts, connection errors,
  5xx, 429).
* POST /hospitals/ is NOT idempotent. A timeout does not tell us whether the
  hospital was stored, so a blind retry can create a duplicate. Creates are
  therefore single-attempt; the processor retries them only after checking
  whether the previous attempt actually landed (see BulkProcessor).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, TypeVar

import httpx

from app.csv_parser import HospitalRow

logger = logging.getLogger(__name__)

T = TypeVar("T")


class UpstreamError(Exception):
    """Base class for failures talking to the Hospital Directory API."""


class UpstreamUnavailable(UpstreamError):
    """Transient failure (timeout, connection error, 5xx, 429): safe to retry."""


class UpstreamRejected(UpstreamError):
    """The upstream refused the request (4xx): retrying will not help."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: bool = True

    def delay(self, attempt: int) -> float:
        """Backoff before retry number `attempt` (1-based)."""
        delay = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        # "Equal jitter": keeps at least half the delay but spreads out
        # concurrent retries so they do not hit the upstream in lockstep.
        return delay / 2 + random.uniform(0, delay / 2) if self.jitter else delay


class DirectoryClient:
    """One instance is shared by every job, so `max_concurrency` bounds the
    total number of in-flight upstream requests for the whole process. Backoff
    sleeps happen outside the limit, so waiting retries don't hold a slot."""

    def __init__(
        self, http: httpx.AsyncClient, retry: RetryPolicy = RetryPolicy(), max_concurrency: int = 20
    ) -> None:
        self._http = http
        self.retry = retry
        self._slots = asyncio.Semaphore(max_concurrency)

    async def create_hospital(self, row: HospitalRow, batch_id: str) -> Dict[str, Any]:
        payload = {
            "name": row.name,
            "address": row.address,
            "phone": row.phone,
            "creation_batch_id": batch_id,
        }
        return await self._request("POST", "/hospitals/", json=payload)

    async def list_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        try:
            return await self._with_retries(lambda: self._request("GET", f"/hospitals/batch/{batch_id}"))
        except UpstreamRejected as exc:
            if exc.status_code == 404:
                return []
            raise

    async def activate_batch(self, batch_id: str) -> int:
        body = await self._with_retries(
            lambda: self._request("PATCH", f"/hospitals/batch/{batch_id}/activate")
        )
        return int(body.get("activated_count", 0))

    async def delete_batch(self, batch_id: str) -> int:
        try:
            body = await self._with_retries(
                lambda: self._request("DELETE", f"/hospitals/batch/{batch_id}")
            )
        except UpstreamRejected as exc:
            if exc.status_code == 404:
                return 0
            raise
        return int(body.get("deleted_count", 0))

    async def ping(self) -> bool:
        """Cheap request that also wakes a cold upstream (e.g. a sleeping Render dyno).

        Any HTTP response, even an error status, proves the upstream is up.
        """
        try:
            await self._request("GET", "/")
        except UpstreamRejected:
            return True
        except UpstreamUnavailable:
            return False
        return True

    async def _with_retries(self, call: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                return await call()
            except UpstreamUnavailable as exc:
                if attempt == self.retry.max_attempts:
                    raise
                delay = self.retry.delay(attempt)
                logger.warning("Transient upstream failure (%s); retry %d in %.2fs", exc, attempt, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._slots:
                response = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning("%s %s timed out", method, path)
            raise UpstreamUnavailable("The Hospital Directory didn't respond in time.") from exc
        except httpx.TransportError as exc:
            logger.warning("%s %s failed: %r", method, path, exc)
            raise UpstreamUnavailable("Couldn't connect to the Hospital Directory.") from exc

        if response.status_code >= 400:
            logger.warning("%s %s returned HTTP %d", method, path, response.status_code)
        if response.status_code >= 500 or response.status_code == 429:
            raise UpstreamUnavailable(
                f"The Hospital Directory returned an error (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            raise _rejected(response)
        return response.json()


def _rejected(response: httpx.Response) -> UpstreamRejected:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = response.text
    if isinstance(detail, list):  # FastAPI/Pydantic validation error list
        detail = "; ".join(str(item.get("msg", item)) for item in detail)
    return UpstreamRejected(
        f"The Hospital Directory rejected this request: {detail} (HTTP {response.status_code}).",
        response.status_code,
    )
