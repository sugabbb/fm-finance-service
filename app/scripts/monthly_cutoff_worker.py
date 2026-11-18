"""Runs the monthly cut-off jobs queued from the frontend (``monthly_cutoff_jobs``): creating a
month's cut-off (then its Excel file) or generating an Excel file. The API only queues them, so
this heavy work never runs in the API process.

    python -m app.scripts.monthly_cutoff_worker            # keep running, poll every 5 s
    python -m app.scripts.monthly_cutoff_worker --once     # run what is queued and exit (cron)

Run it as a service next to the API. Several workers can run at once: each job is taken by one
(``FOR UPDATE SKIP LOCKED``). Without a worker, the hourly ``monthly_cutoff --pending`` run picks
up the queued jobs, so they wait up to an hour instead of a few seconds.

A job whose worker dies (killed, host restarted) is marked interrupted after
``CUTOFF_JOB_TIMEOUT_MINUTES`` and the month can be queued again. SIGTERM/SIGINT stop the worker
after the current job.
"""

import argparse
import logging
import signal
import sys
import time
from collections.abc import Sequence
from types import FrameType

from app.core.config import settings
from app.features.monthly_cutoff_jobs import run_queued

logger = logging.getLogger("app.scripts.monthly_cutoff_worker")


class Stop:
    requested = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        logger.info("signal %s received; stopping after the current job", signum)
        self.requested = True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.monthly_cutoff_worker",
        description="Run the monthly cut-off and Excel export jobs queued from the frontend.",
    )
    parser.add_argument("--once", action="store_true", help="run the queued jobs once and exit")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between polls (default 5)")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be > 0")

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        try:
            # One job per round, so a stop request is honoured between jobs.
            while not stop.requested and run_queued(limit=1):
                pass
        except Exception:
            logger.exception("running the monthly cut-off jobs failed; retrying")
            if args.once:
                return 1
        if args.once or stop.requested:
            return 0
        deadline = time.monotonic() + args.interval
        while not stop.requested and time.monotonic() < deadline:
            time.sleep(min(0.2, args.interval))
        if stop.requested:
            return 0


if __name__ == "__main__":
    sys.exit(main())
