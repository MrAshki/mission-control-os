"""Smoke tests for the public plugin API surface.

These assert response status and the stable top-level fields of GET /health,
GET /runtime, and GET /services with all local Hermes/runtime discovery
mocked (see conftest.py).
"""
from __future__ import annotations

EXPECTED_SERVICE_KEYS = {
    "dashboard",
    "plugin",
    "hermes_cli",
    "gateway",
    "memory_vault",
    "claude_code_cli",
    "whisper",
    "piper",
}


def test_health_reports_ok_with_stable_fields(client, plugin_api):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["version"] == plugin_api.PLUGIN_VERSION
    assert isinstance(body["generated_at"], float)
    assert body["interaction_mode"] == "hermes_in_process_native_session"
    # No Hermes CLI is available in the test environment.
    assert body["runtime_state"] == "offline"
    assert body["hermes"]["availability"] == "unavailable"
    assert set(body["health"]) == EXPECTED_SERVICE_KEYS
    assert body["health"]["plugin"] == "online"


def test_runtime_returns_full_snapshot_shape(client, plugin_api):
    res = client.get("/runtime")
    assert res.status_code == 200
    body = res.json()
    for key in ("version", "generated_at", "runtime_state", "hermes", "model", "health", "services", "hardware", "telemetry"):
        assert key in body, f"missing top-level field: {key}"
    assert body["version"] == plugin_api.PLUGIN_VERSION
    assert set(body["services"]) == EXPECTED_SERVICE_KEYS
    assert body["services"]["plugin"] == {"available": True, "status": "online", "version": plugin_api.PLUGIN_VERSION}
    assert body["hermes"]["status"] == "unavailable"
    assert body["telemetry"]["mission_control_session_id"] is None


def test_services_lists_dependency_availability(client, plugin_api):
    res = client.get("/services")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["version"] == plugin_api.PLUGIN_VERSION
    assert set(body["services"]) == EXPECTED_SERVICE_KEYS
    for service in body["services"].values():
        assert "status" in service
    assert "hardware" in body
