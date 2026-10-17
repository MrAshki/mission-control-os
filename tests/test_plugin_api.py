"""Smoke tests for GET /health, GET /runtime, GET /services.

All local Hermes/runtime discovery is mocked (see conftest.py), so these
tests assert response status and the *shape* of each response rather than
live values.
"""
from __future__ import annotations

EXPECTED_SERVICE_KEYS = {
    "dashboard", "plugin", "hermes_cli", "gateway",
    "memory_vault", "claude_code_cli", "whisper", "piper",
}


def test_health_ok_with_expected_fields(api_client, plugin_api_module):
    response = api_client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["version"] == plugin_api_module.PLUGIN_VERSION
    assert isinstance(body["generated_at"], float)
    assert body["interaction_mode"] == "hermes_in_process_native_session"
    assert body["runtime_state"] == "offline"  # no hermes_cli available in test env
    assert body["hermes"]["availability"] == "unavailable"
    assert set(body["health"]) == EXPECTED_SERVICE_KEYS
    assert body["health"]["plugin"] == "online"


def test_runtime_returns_full_snapshot(api_client, plugin_api_module):
    response = api_client.get("/runtime")
    assert response.status_code == 200

    body = response.json()
    expected_top_level = {
        "version", "generated_at", "runtime_state", "hermes",
        "model", "health", "services", "hardware", "telemetry",
    }
    assert expected_top_level.issubset(body.keys())
    assert body["version"] == plugin_api_module.PLUGIN_VERSION
    assert set(body["services"]) == EXPECTED_SERVICE_KEYS
    assert body["services"]["plugin"]["status"] == "online"
    assert body["hermes"]["status"] == "unavailable"


def test_services_lists_dependency_availability(api_client):
    response = api_client.get("/services")
    assert response.status_code == 200

    body = response.json()
    assert body["ok"] is True
    assert set(body["services"]) == EXPECTED_SERVICE_KEYS
    for name, service in body["services"].items():
        assert "status" in service, f"{name} is missing a status field"
    assert "hardware" in body