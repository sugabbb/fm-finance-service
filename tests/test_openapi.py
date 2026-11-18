from fastapi.testclient import TestClient

from app.main import app


def test_health_and_openapi_security_and_exclusions() -> None:
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/auth/login" not in paths
    assert "/api/v1/employees" not in paths
    assert paths["/api/v1/revenues"]["get"]["security"] == [{"BearerAuth": []}]
    assert "/dashboard" not in paths
    assert all("report" not in path and "abk" not in path and "growth" not in path for path in paths)


def test_swagger_is_available() -> None:
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 404
