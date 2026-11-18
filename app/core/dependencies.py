from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import AppError, forbidden
from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
DbSession = Annotated[Session, Depends(get_db)]


def current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise AppError(401, "INVALID_TOKEN", "Authentication credentials are invalid.")
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = int(str(payload["sub"]))
        version = int(str(payload["token_version"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError(401, "INVALID_TOKEN", "Authentication credentials are invalid.") from exc
    # Operational service is the sole user authority. The finance copy is a projection used only
    # for audit FKs and display names; it is refreshed on every authenticated request.
    from app.integrations.operational import operational_client

    authoritative = operational_client.authorize(user_id, version)
    user = db.get(User, user_id)
    if user is None:
        user = User(id=user_id, password_hash="!managed-by-operational-service!")
        db.add(user)
    user.name = str(authoritative["name"])
    user.email = str(authoritative["email"])
    user.role = str(authoritative["role"])
    user.is_active = bool(authoritative["is_active"])
    user.must_change_password = bool(authoritative["must_change_password"])
    user.token_version = int(authoritative["token_version"])
    user.synced_at = datetime.now(UTC)
    db.flush()
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def active_user(user: CurrentUser) -> User:
    if user.must_change_password:
        raise AppError(403, "PASSWORD_CHANGE_REQUIRED", "Password must be changed before using this endpoint.")
    return user


ActiveUser = Annotated[User, Depends(active_user)]


def superadmin(user: ActiveUser) -> User:
    if user.role != "SUPERADMIN":
        raise forbidden()
    return user


Superadmin = Annotated[User, Depends(superadmin)]

# Listed explicitly (today these are all the roles), so a role added later does not silently get
# access to the finance figures.
FINANCE_ROLES = frozenset({"ADMIN", "SUPERADMIN"})


def finance_reader(user: ActiveUser) -> User:
    if user.role not in FINANCE_ROLES:
        raise forbidden()
    return user


FinanceReader = Annotated[User, Depends(finance_reader)]
