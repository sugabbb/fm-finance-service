"""The month-end reads of the operational client (app/integrations/operational.py), at the HTTP
level: the path and key sent, 404 as "not finalized yet" and every other failure as an error."""

from collections.abc import Callable

import httpx
import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.operational import OperationalNotFound, operational_client

BODY = {"year": 2026, "month": 7, "revision": 1, "finalized_at": "2026-08-01T08:00:00+07:00", "lines": []}


def answer(monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    sent: list[httpx.Request] = []
    transport = httpx.MockTransport(respond)

    def request(method: str, url: str, **kwargs: object) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            response = client.request(method, url, headers=kwargs.get("headers"), json=kwargs.get("json"))  # type: ignore[arg-type]
        sent.append(response.request)
        return response

    monkeypatch.setattr("app.integrations.operational.httpx.request", request)
    return sent


def test_the_month_end_data(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = answer(monkeypatch, lambda request: httpx.Response(200, json={"data": BODY}))

    assert operational_client.month_end_egg_stock(2026, 7) == BODY
    assert operational_client.month_end_fish_weight_estimate(2026, 7) == BODY

    assert [(request.method, request.url.path) for request in sent] == [
        ("GET", "/api/v1/internal/month-end/2026/7/egg-stock"),
        ("GET", "/api/v1/internal/month-end/2026/7/fish-weight-estimate"),
    ]
    assert sent[0].headers["X-FM-Internal-Key"] == settings.operational_internal_api_key


def test_404_means_not_finalized_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"code": "MONTH_END_NOT_FINALIZED", "detail": "Not finalized."}
    answer(monkeypatch, lambda request: httpx.Response(404, json=body))

    with pytest.raises(OperationalNotFound) as not_found:
        operational_client.month_end_egg_stock(2026, 7)

    error = not_found.value
    assert (error.path, error.code, error.detail) == (
        "/api/v1/internal/month-end/2026/7/egg-stock",
        "MONTH_END_NOT_FINALIZED",
        "Not finalized.",
    )


def test_404_without_a_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """E.g. a proxy, or an operational-service without the endpoint yet: still "not available"."""
    answer(monkeypatch, lambda request: httpx.Response(404, text="Not Found"))

    with pytest.raises(OperationalNotFound) as not_found:
        operational_client.month_end_fish_weight_estimate(2026, 7)

    assert (not_found.value.code, not_found.value.detail) == (None, None)


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, {"detail": "boom"}), (401, {"code": "INVALID_INTERNAL_KEY"}), (200, {"data": None}), (200, {"data": []})],
)
def test_other_answers_are_errors(monkeypatch: pytest.MonkeyPatch, status: int, body: object) -> None:
    answer(monkeypatch, lambda request: httpx.Response(status, json=body))

    with pytest.raises(AppError) as error:
        operational_client.month_end_egg_stock(2026, 7)

    assert (error.value.status_code, error.value.code) == (502, "OPERATIONAL_SERVICE_ERROR")


def test_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    answer(monkeypatch, refuse)

    with pytest.raises(AppError) as error:
        operational_client.month_end_egg_stock(2026, 7)

    assert (error.value.status_code, error.value.code) == (503, "OPERATIONAL_SERVICE_UNAVAILABLE")
