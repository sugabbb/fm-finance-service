from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError


class OperationalNotFound(Exception):
    """Operational answered ``404`` to a read that allows it (``allow_not_found``)."""

    def __init__(self, path: str, code: str | None, detail: str | None) -> None:
        super().__init__(f"{path}: 404 {code or '-'} {detail or ''}".rstrip())
        self.path = path
        self.code = code
        self.detail = detail


class OperationalClient:
    def _request(
        self, method: str, path: str, *, json: object | None = None, allow_not_found: bool = False
    ) -> dict[str, Any]:
        try:
            response = httpx.request(
                method,
                f"{settings.operational_service_url.rstrip('/')}{path}",
                headers={"X-FM-Internal-Key": settings.operational_internal_api_key},
                json=json,
                timeout=settings.operational_timeout_seconds,
            )
        except httpx.RequestError as exc:
            raise AppError(503, "OPERATIONAL_SERVICE_UNAVAILABLE", "Operational service is unavailable.") from exc
        if response.status_code in (401, 403) and path == "/api/v1/internal/auth/authorize":
            body = response.json()
            raise AppError(response.status_code, body.get("code", "INVALID_TOKEN"), body.get("detail", "Access denied."))
        if response.status_code == 404 and allow_not_found:
            try:
                body = response.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                body = {}
            raise OperationalNotFound(path, body.get("code"), body.get("detail"))
        if response.is_error:
            raise AppError(
                502,
                "OPERATIONAL_SERVICE_ERROR",
                "Operational service rejected the internal request.",
                extra={"upstream_status": response.status_code},
            )
        return response.json()

    def authorize(self, user_id: int, token_version: int) -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/internal/auth/authorize", json={"user_id": user_id, "token_version": token_version}
        )["data"]

    def resolve_feed_types(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        body = self._request("POST", "/api/v1/internal/references/feed-types/resolve", json={"ids": ids})
        if body.get("missing_ids"):
            raise AppError(422, "VALIDATION_ERROR", "The request is invalid.", errors={"details.feed_type_id": ["Feed type does not exist."]})
        return {int(row["id"]): row for row in body["data"]}

    def employee(self, employee_id: int) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/internal/references/employees/{employee_id}")["data"]

    def month_end_egg_stock(self, year: int, month: int) -> dict[str, Any]:
        """The closing egg stock of a month. Raises :class:`OperationalNotFound` while operational
        has not finalized the month (see docs/operational-month-end-api.md)."""
        return self._month_end(year, month, "egg-stock")

    def month_end_fish_weight_estimate(self, year: int, month: int) -> dict[str, Any]:
        """The month-end fish weight estimate per pond; :class:`OperationalNotFound` while not finalized."""
        return self._month_end(year, month, "fish-weight-estimate")

    def _month_end(self, year: int, month: int, resource: str) -> dict[str, Any]:
        body = self._request("GET", f"/api/v1/internal/month-end/{year}/{month}/{resource}", allow_not_found=True)
        data = body.get("data")
        if not isinstance(data, dict):
            raise AppError(502, "OPERATIONAL_SERVICE_ERROR", f"Operational service returned no data for {resource}.")
        return data

    def put_inventory(self, path: str, payload: dict[str, Any]) -> None:
        self._request("PUT", path, json=payload)


operational_client = OperationalClient()
