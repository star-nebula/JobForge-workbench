"""HTTP/service boundary tests for health, auth, request IDs, and error envelopes."""
import re
import urllib.request

import pytest
from fastapi.testclient import TestClient

from jobforge import server
from jobforge.observability import JsonFormatter
from jobforge.settings import get_settings


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("JOBFORGE_API_KEY", raising=False)
    with TestClient(server.app) as test_client:
        yield test_client


def test_health_live_is_public_and_correlated(client):
    r = client.get("/health/live")

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["service"] == "jobforge"
    assert r.headers["X-Request-ID"] == r.json()["request_id"]


def test_health_ready_checks_database(client):
    r = client.get("/health/ready")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["status"] == "up"
    assert body["checks"]["database"]["engine"] == "sqlite"


def test_validation_errors_use_one_envelope(client):
    r = client.get("/api/jobs", params={"all_cities": "definitely-not-bool"})

    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"] == r.headers["X-Request-ID"]
    assert body["detail"] == body["error"]["message"]
    assert "definitely-not-bool" not in r.text


def test_http_exceptions_keep_legacy_detail_and_add_error_envelope(client):
    r = client.get("/api/jobs/boss/missing")

    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "job not found"
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "job not found"
    assert body["error"]["request_id"] == r.headers["X-Request-ID"]


def test_optional_api_key_protects_api_routes(client, monkeypatch):
    monkeypatch.setenv("JOBFORGE_API_KEY", "test-secret")

    denied = client.get("/api/platforms")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "unauthorized"

    wrong = client.get("/api/platforms", headers={"X-API-Key": "wrong"})
    assert wrong.status_code == 401

    allowed = client.get("/api/platforms", headers={"X-API-Key": "test-secret"})
    assert allowed.status_code == 200
    assert allowed.json()["platforms"][0]["key"] == "boss"

    bearer = client.get("/api/platforms",
                        headers={"Authorization": "Bearer test-secret"})
    assert bearer.status_code == 200


def test_health_checks_stay_public_when_api_key_is_enabled(client, monkeypatch):
    monkeypatch.setenv("JOBFORGE_API_KEY", "test-secret")

    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_readiness_failure_is_reported_without_leaking_exception_text(client, monkeypatch):
    def unavailable():
        raise RuntimeError("secret database path")

    monkeypatch.setattr(server.db, "healthcheck", unavailable)
    r = client.get("/health/ready")

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "secret database path" not in r.text
    assert r.headers["X-Request-ID"] == r.json()["error"]["request_id"]


def test_request_id_accepts_safe_values_and_rejects_untrusted_format(client):
    safe = client.get("/health/live", headers={"X-Request-ID": "req-123.safe"})
    assert safe.headers["X-Request-ID"] == "req-123.safe"

    unsafe = client.get("/health/live", headers={"X-Request-ID": "bad id!"})
    assert unsafe.headers["X-Request-ID"] != "bad id!"
    assert re.fullmatch(r"[0-9a-f]{32}", unsafe.headers["X-Request-ID"])


def test_json_log_formatter_keeps_operational_fields():
    import json
    import logging

    record = logging.LogRecord(
        "jobforge.test", logging.INFO, __file__, 1, "request complete", (), None
    )
    record.request_id = "req-1"
    record.status_code = 200

    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "request complete"
    assert payload["request_id"] == "req-1"
    assert payload["status_code"] == 200


def test_settings_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("JOBFORGE_ENV", "test")
    monkeypatch.setenv("JOBFORGE_HOST", "0.0.0.0")
    monkeypatch.setenv("JOBFORGE_PORT", "19090")
    monkeypatch.setenv("JOBFORGE_API_KEY", "secret")
    monkeypatch.setenv("JOBFORGE_LOG_FORMAT", "text")
    monkeypatch.setenv("JOBFORGE_CORS_ORIGINS", "https://one.example, https://two.example")

    settings = get_settings()
    assert settings.environment == "test"
    assert settings.host == "0.0.0.0"
    assert settings.port == 19090
    assert settings.auth_enabled is True
    assert settings.log_format == "text"
    assert settings.cors_origins == ("https://one.example", "https://two.example")


def test_settings_reject_invalid_values(monkeypatch):
    monkeypatch.setenv("JOBFORGE_PORT", "not-a-port")
    with pytest.raises(ValueError, match="JOBFORGE_PORT"):
        get_settings()

    monkeypatch.setenv("JOBFORGE_PORT", "8080")
    monkeypatch.setenv("JOBFORGE_LOG_FORMAT", "xml")
    with pytest.raises(ValueError, match="JOBFORGE_LOG_FORMAT"):
        get_settings()


def test_internal_probes_use_the_configured_api_key(monkeypatch):
    seen = {}

    class Response:
        status = 200

        def read(self):
            return b'{"app":"jobforge"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setenv("JOBFORGE_API_KEY", "secret")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    status, body = server._http_get(18080, "/api/whoami")
    assert status == 200
    assert body == b'{"app":"jobforge"}'
    assert seen["headers"]["x-api-key"] == "secret"
    assert seen["timeout"] == 3
