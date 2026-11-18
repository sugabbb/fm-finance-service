"""Posts queued main cash account movements (``main_cash_account_events``) to the log.

The API already posts them right after each cashflow/revenue response (FastAPI background task),
and the daily audit drains whatever is left. Run this when you want a dedicated consumer, for
example on hosts where work after the response is not reliable (CPU throttled or instances shut
down once the response is sent):

    python -m app.scripts.main_cash_account_worker            # keep running, poll every 5 s
    python -m app.scripts.main_cash_account_worker --once     # drain the queue and exit (cron)

Several consumers can run at once; they serialise on the account row and never post an event
twice.
"""

import argparse
import logging
import signal
import sys
import time
from collections.abc import Sequence
from types import FrameType

from app.core.config import settings
from app.features.main_cash_account_events import DEFAULT_BATCH_SIZE, drain

logger = logging.getLogger("app.scripts.main_cash_account_worker")


class Stop:
    requested = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        logger.info("signal %s received; stopping after the current batch", signum)
        self.requested = True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.main_cash_account_worker",
        description="Post queued main cash account movements to main_cash_account_logs.",
    )
    parser.add_argument("--once", action="store_true", help="drain the queue once and exit")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between polls (default 5)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.interval <= 0:
        parser.error("--batch-size must be >= 1 and --interval > 0")

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        try:
            posted = drain(batch_size=args.batch_size)
            if posted:
                logger.info("posted %s main cash account events", posted)
        except Exception:
            logger.exception("posting main cash account events failed; retrying")
            if args.once:
                return 1
        if args.once:
            return 0
        deadline = time.monotonic() + args.interval
        while not stop.requested and time.monotonic() < deadline:
            time.sleep(min(0.2, args.interval))
        if stop.requested:
            return 0


if __name__ == "__main__":
    sys.exit(main())
