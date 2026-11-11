"""Browser test fixtures: a live server on a free port plus a Chrome page.

The server runs the real app against ScriptedDirectory, so hospital-name
tags (e.g. "[server-error]") drive failure scenarios exactly as in the demo
server. Tests skip cleanly when Playwright or Google Chrome is unavailable.
"""

import socket
import threading
import time

import pytest
import uvicorn

from app.config import Settings
from app.main import create_app
from tests.fake_directory import BASE_URL, ScriptedDirectory

playwright_api = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def directory():
    return ScriptedDirectory(latency=0.15, attempts_per_run=3)


@pytest.fixture
def base_url(directory):
    settings = Settings(
        upstream_base_url=BASE_URL,
        warm_upstream_on_startup=False,
        retry_base_delay=0,
        retry_max_delay=0,
        log_level="WARNING",
    )
    app = create_app(settings, transport=directory.transport())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning", ws="none"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


# Module scope (not session): Playwright's sync API marks an asyncio loop as
# running on this thread while open, which would break the async unit tests
# that run after this module.
@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception as exc:  # Chrome not installed on this machine
            pytest.skip(f"Google Chrome unavailable: {exc}")
        yield browser
        browser.close()


@pytest.fixture
def page(browser, base_url):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(base_url)
    yield page
    context.close()
    assert errors == [], f"browser console errors: {errors}"
