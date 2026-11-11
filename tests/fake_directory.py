"""A stateful, in-process stand-in for the Hospital Directory API.

Plugged into httpx via MockTransport so the real client code (URL building,
JSON handling, error mapping) is exercised end to end, while tests can script
failures per hospital name:

    "500"            -> upstream returns HTTP 500, nothing is stored
    "422"            -> upstream rejects the payload
    "timeout"        -> request times out before reaching the upstream
    "commit_timeout" -> hospital IS stored, but the response times out
                        (the ambiguous case that makes naive retries duplicate)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List

import httpx

BASE_URL = "https://directory.test"


class FakeDirectory:
    def __init__(self, latency: float = 0.0) -> None:
        self.latency = latency
        self.hospitals: Dict[int, dict] = {}
        self.create_failures: Dict[str, List[str]] = defaultdict(list)
        self.activate_failures: List[str] = []
        self.requests: List[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.transport_down = False  # simulate the whole directory being unreachable
        self._next_id = 1

    # -- failure scripting -------------------------------------------------
    def fail_create(self, name: str, *outcomes: str) -> None:
        self.create_failures[name].extend(outcomes)

    def fail_activate(self, *outcomes: str) -> None:
        self.activate_failures.extend(outcomes)

    # -- inspection helpers ------------------------------------------------
    def batch(self, batch_id: str) -> List[dict]:
        return [h for h in self.hospitals.values() if h["creation_batch_id"] == batch_id]

    def count(self, method_and_path_prefix: str) -> int:
        return sum(1 for r in self.requests if r.startswith(method_and_path_prefix))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=BASE_URL, transport=self.transport())

    # -- request handling --------------------------------------------------
    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if self.transport_down:
            raise httpx.ConnectError("simulated outage", request=request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.latency:
                await asyncio.sleep(self.latency)
            return self._route(request)
        finally:
            self.in_flight -= 1

    def _route(self, request: httpx.Request) -> httpx.Response:
        parts = [p for p in request.url.path.split("/") if p]
        method = request.method
        if parts == ["hospitals"] and method == "POST":
            return self._create(request)
        if parts == ["hospitals"] and method == "GET":
            return httpx.Response(200, json=list(self.hospitals.values()))
        if len(parts) == 3 and parts[:2] == ["hospitals", "batch"]:
            if method == "GET":
                found = self.batch(parts[2])
                if not found:
                    return _not_found()
                return httpx.Response(200, json=found)
            if method == "DELETE":
                found = self.batch(parts[2])
                if not found:
                    return _not_found()
                for h in found:
                    del self.hospitals[h["id"]]
                return httpx.Response(200, json={"deleted_count": len(found), "message": "ok"})
        if len(parts) == 4 and parts[3] == "activate" and method == "PATCH":
            return self._activate(request, parts[2])
        return httpx.Response(404, json={"detail": "Not Found"})

    def _create(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        name = payload.get("name", "")
        outcome = self.create_failures[name].pop(0) if self.create_failures[name] else "ok"
        if outcome == "500":
            return httpx.Response(500, text="Internal Server Error")
        if outcome == "422":
            return httpx.Response(422, json={"detail": [{"msg": "rejected by upstream"}]})
        if outcome == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)

        hospital = {
            "id": self._next_id,
            "name": name,
            "address": payload["address"],
            "phone": payload.get("phone"),
            "creation_batch_id": payload.get("creation_batch_id"),
            "active": payload.get("creation_batch_id") is None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.hospitals[self._next_id] = hospital
        self._next_id += 1
        if outcome == "commit_timeout":
            raise httpx.ReadTimeout("simulated timeout after commit", request=request)
        return httpx.Response(200, json=hospital)

    def _activate(self, request: httpx.Request, batch_id: str) -> httpx.Response:
        if self.activate_failures:
            outcome = self.activate_failures.pop(0)
            if outcome == "timeout":
                raise httpx.ReadTimeout("simulated timeout", request=request)
            return httpx.Response(int(outcome), text="error")
        found = self.batch(batch_id)
        if not found:
            return _not_found()
        for h in found:
            h["active"] = True
        return httpx.Response(
            200, json={"activated_count": len(found), "message": f"Activated {len(found)}"}
        )


class ScriptedDirectory(FakeDirectory):
    """FakeDirectory whose failures come from tags at the end of hospital names.

    Used by the demo server and the browser tests:
        [server-error]      HTTP 500 for the first `attempts_per_run` attempts
        [rejected]          HTTP 422 on the first attempt
        [flaky]             HTTP 500 on the first attempt
        [slow-save]         stored, but the first response times out
        [activation-fails]  activating the batch fails `attempts_per_run` times
    """

    def __init__(self, latency: float, attempts_per_run: int) -> None:
        super().__init__(latency=latency)
        self.attempts_per_run = attempts_per_run
        self.create_attempts: Counter = Counter()
        self.activation_attempts: Counter = Counter()
        self.flaky_activation_batches: set = set()

    def _create(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        name, batch_id = payload.get("name", ""), payload.get("creation_batch_id")
        key = (batch_id, name)
        self.create_attempts[key] += 1
        attempt = self.create_attempts[key]

        if name.endswith("[server-error]") and attempt <= self.attempts_per_run:
            self.fail_create(name, "500")
        elif name.endswith("[rejected]") and attempt == 1:
            self.fail_create(name, "422")
        elif name.endswith("[flaky]") and attempt == 1:
            self.fail_create(name, "500")
        elif name.endswith("[slow-save]") and attempt == 1:
            self.fail_create(name, "commit_timeout")
        elif name.endswith("[activation-fails]"):
            self.flaky_activation_batches.add(batch_id)
        return super()._create(request)

    def _activate(self, request: httpx.Request, batch_id: str) -> httpx.Response:
        if batch_id in self.flaky_activation_batches:
            self.activation_attempts[batch_id] += 1
            if self.activation_attempts[batch_id] <= self.attempts_per_run:
                self.fail_activate("500")
        return super()._activate(request, batch_id)


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"detail": "No hospitals found with the specified batch ID"})


def new_batch_id() -> str:
    return str(uuid.uuid4())
