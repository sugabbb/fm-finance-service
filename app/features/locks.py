from sqlalchemy import text
from sqlalchemy.orm import Session

ADVISORY_LOCK = 20261009


def lock_balances(db: Session) -> None:
    db.execute(text("select pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK})
