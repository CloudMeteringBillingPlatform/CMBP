"""Worker process: stream consumer, recovery loop, quota job, storage sampler, billing scheduler."""

import asyncio
import logging
import signal

from app import db
from app.config import settings
from app.billing.periods import close_ended_periods, ensure_current_periods
from app.jobs import sampler
from app.jobs.quotas import check_quotas
from app.pipeline.worker import Worker
from app.redis_client import close_redis

log = logging.getLogger("worker.main")


async def every(name: str, seconds: float, job, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await job()
        except Exception:  # noqa: BLE001 - one failed run must not stop the schedule
            log.exception("%s failed", name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


async def billing_tick() -> None:
    await ensure_current_periods()
    await close_ended_periods()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.open_pool(min_size=1, max_size=6)
    worker = Worker()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: (stop.set(), worker.stop()))

    tasks = [asyncio.create_task(worker.run())]
    if settings.run_scheduled_jobs:
        tasks += [
            asyncio.create_task(every("quotas", settings.quota_interval_seconds, check_quotas, stop)),
            asyncio.create_task(every("sampler", settings.sampler_window_seconds, sampler.run_once, stop)),
            asyncio.create_task(every("billing", settings.billing_interval_seconds, billing_tick, stop)),
        ]
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await close_redis()
    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
