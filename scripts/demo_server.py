"""Run the app against a simulated Hospital Directory that fails on purpose.

The real Directory accepts every row our validation accepts, so failure
handling (retries, resume, rollback, activation failure) can only be seen
end to end against a simulation:

    .venv/bin/python scripts/demo_server.py      # then open http://localhost:8001

A tag at the end of a hospital's name decides what happens to it:

    [server-error]      HTTP 500 on every attempt of the first run; the retry succeeds
    [rejected]          HTTP 422 on the first run; the retry succeeds
    [flaky]             HTTP 500 once; the automatic retry succeeds
    [slow-save]         stored, but the response times out; recovered without a duplicate
    [activation-fails]  the batch can't be activated until you retry it

Every request takes about 1.5 s so progress is easy to watch. Data lives in
memory and disappears when the server stops.
"""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fake_directory import BASE_URL, ScriptedDirectory  # noqa: E402

PORT = 8001


def main() -> None:
    settings = Settings(
        upstream_base_url=BASE_URL,
        warm_upstream_on_startup=False,
        retry_base_delay=0.3,
        retry_max_delay=1.0,
    )
    directory = ScriptedDirectory(latency=1.5, attempts_per_run=settings.max_attempts)
    app = create_app(settings, transport=directory.transport())
    print(f"Demo mode: simulated Hospital Directory. Open http://localhost:{PORT}")
    uvicorn.run(app, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
