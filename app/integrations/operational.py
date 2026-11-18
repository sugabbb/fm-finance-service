from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError


class OperationalClient:
    def _request(self, method: str, path: str, *, json: object | None = None) -> dict[str, Any]:
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

    def put_inventory(self, path: str, payload: dict[str, Any]) -> None:
        self._request("PUT", path, json=payload)


operational_client = OperationalClient()
