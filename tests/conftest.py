"""Fixtures for the plugin health API smoke tests.

plugin_api.py reads several HERMES_* environment variables at import time
to locate the Hermes home, vault, and voice-model directories. To test it
without a real Hermes installation, we point those variables at temporary
directories *before* importing the module, and we replace every function
that would otherwise probe the real machine (PATH lookups, port scans,
subprocess calls, hardware sensors) with a fake that reports "unavailable".
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
def plugin_api_module(tmp_path_factory):
    """Import plugin_api.py with all HERMES_* paths pointed at temp dirs."""
    hermes_home = tmp_path_factory.mktemp("hermes_home")
    vault = tmp_path_factory.mktemp("vault")
    whisper_dir = hermes_home / "whisper"
    piper_dir = hermes_home / "piper"
    whisper_dir.mkdir()
    piper_dir.mkdir()

    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ["HERMES_AGENT_CORE"] = str(hermes_home / "hermes-agent")
    os.environ["HERMES_MISSION_CONTROL_VAULT"] = str(vault)
    os.environ["HERMES_MISSION_CONTROL_WORKDIR"] = str(vault)
    os.environ["HERMES_MISSION_CONTROL_WHISPER_DIR"] = str(whisper_dir)
    os.environ["HERMES_MISSION_CONTROL_PIPER_DIR"] = str(piper_dir)

    spec = importlib.util.spec_from_file_location(
        "plugin_api_under_test", REPO_ROOT / "dashboard" / "plugin_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def api_client(plugin_api_module, monkeypatch):
    """A TestClient with all machine-touching probes stubbed to 'unavailable'."""
    unavailable = {"status": "unavailable", "source": "test stub"}

    monkeypatch.setattr(
        plugin_api_module, "_command_available",
        lambda command: {"available": False, "path": None},
    )
    monkeypatch.setattr(
        plugin_api_module, "_dashboard_available",
        lambda: {"available": False, "url": "http://127.0.0.1:9119"},
    )
    monkeypatch.setattr(
        plugin_api_module, "_gateway_status",
        lambda: {"available": False, **unavailable},
    )
    monkeypatch.setattr(
        plugin_api_module, "_hermes_status",
        lambda: {"available": False, **unavailable},
    )
    monkeypatch.setattr(
        plugin_api_module, "_hardware_telemetry",
        lambda: {
            "gpu": dict(unavailable),
            "cpu": dict(unavailable),
            "memory": dict(unavailable),
            "hermes_processes": dict(unavailable),
            "polling_hint_seconds": {"gpu_cpu_memory": 2, "expensive_sensors": 10},
        },
    )

    app = FastAPI()
    app.include_router(plugin_api_module.router)
    return TestClient(app)