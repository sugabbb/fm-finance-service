import argparse
import logging
import time

from app.db.session import SessionLocal
from app.features.operational_outbox import deliver_pending


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver finance inventory projections to operational-service.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    while True:
        with SessionLocal() as db:
            delivered = deliver_pending(db)
        logging.info("delivered %s operational projection(s)", delivered)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
