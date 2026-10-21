"""Run job-queue workers without the HTTP API: `python -m app.worker`. Use for extra capacity, with
WORKER_ENABLED=0 on policy-api if you want the API to do no checks itself."""
from __future__ import annotations

import asyncio
import logging
import signal

from .config import load_settings
from .jobs import Worker
from .services import build_services

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def main() -> None:
    settings = load_settings()
    if settings.demo_mode:
        raise SystemExit("A separate worker can't share demo mode's in-memory data. Use the API's built-in worker.")
    svc = await build_services(settings)
    worker = Worker(svc)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    task = asyncio.create_task(worker.run_forever())
    await stop.wait()
    await worker.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await svc.close()


if __name__ == "__main__":
    asyncio.run(main())
