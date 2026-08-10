"""Shared fixtures for the plugin API smoke tests.

dashboard/plugin_api.py resolves the Hermes home, vault, and voice asset
directories from environment variables at import time, so the fake locations
must be exported before the module is loaded. Every discovery probe that
would otherwise touch the developer machine (CLI lookups, port scans,
subprocesses, hardware sensors) is replaced with static data, so the suite
runs without a live Hermes installation.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def plugin_api(tmp_path_factory):
    home = tmp_path_factory.mktemp("hermes-home")
    vault = tmp_path_factory.mktemp("vault")
    (home / "whisper").mkdir()
    (home / "piper").mkdir()
    os.environ.update({
        "HERMES_HOME": str(home),
        "HERMES_AGENT_CORE": str(home / "hermes-agent"),
        "HERMES_MISSION_CONTROL_VAULT": str(vault),
        "HERMES_MISSION_CONTROL_WORKDIR": str(vault),
        "HERMES_MISSION_CONTROL_WHISPER_DIR": str(home / "whisper"),
        "HERMES_MISSION_CONTROL_PIPER_DIR": str(home / "piper"),
    })
    spec = importlib.util.spec_from_file_location(
        "mission_control_plugin_api", REPO_ROOT / "dashboard" / "plugin_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def client(plugin_api, monkeypatch):
    stub = {"status": "unavailable", "source": "test stub"}
    monkeypatch.setattr(plugin_api, "_command_available", lambda command: {"available": False, "path": None})
    monkeypatch.setattr(plugin_api, "_dashboard_available", lambda: {"available": False, "url": "http://127.0.0.1:9119"})
    monkeypatch.setattr(plugin_api, "_gateway_status", lambda: {"available": False, **stub})
    monkeypatch.setattr(plugin_api, "_hermes_status", lambda: {"available": False, **stub})
    monkeypatch.setattr(plugin_api, "_hardware_telemetry", lambda: {
        "gpu": dict(stub),
        "cpu": dict(stub),
        "memory": dict(stub),
        "hermes_processes": dict(stub),
        "polling_hint_seconds": {"gpu_cpu_memory": 2, "expensive_sensors": 10},
    })
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)
