"""Hermes OS Mission Control dashboard plugin API.

V5 API. Mounted at /api/plugins/hermes-os-mission-control/.
Phase 2 connects /talk to Hermes native CLI sessions. Hermes state.db remains
the canonical conversation store; plugin-local state only stores UI metadata
and the stable Mission Control session pointer.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

router = APIRouter()
PLUGIN_VERSION = "0.7.0"
LIVE_BRIDGE_TIMEOUT_SECONDS = 180
IN_PROCESS_RUNNER_TIMEOUT_SECONDS = 420
HERMES_CORE = Path(os.environ.get("HERMES_AGENT_CORE", str(Path(get_hermes_home()) / "hermes-agent")))
DEFAULT_VAULT = Path(
    os.environ.get("HERMES_MISSION_CONTROL_VAULT")
    or os.environ.get("OBSIDIAN_VAULT_PATH")
    or os.environ.get("HERMES_VAULT_PATH")
    or str(Path.home() / "Memory")
)
APPROVED_SCOPE = Path(os.environ.get("HERMES_MISSION_CONTROL_WORKDIR", str(DEFAULT_VAULT)))
OS_NOTE_NAMES = {
    "kernel": "Hermes OS Kernel.md",
    "state": "Hermes OS State.md",
    "focus": "Hermes OS Current Focus.md",
    "task_inbox": "Hermes OS Task Inbox.md",
    "approvals": "Hermes OS Approval Workflow.md",
    "memory_routing": "Hermes OS Memory Routing.md",
    "project_registry": "Hermes OS Project Registry.md",
    "mission_control": "Hermes OS Mission Control Spec.md",
    "constitution": "Hermes OS Constitution.md",
}

class TalkBody(BaseModel):
    message: str
    new_conversation: bool = False



def _home() -> Path:
    return Path(get_hermes_home()).expanduser()


def _state_json_path() -> Path:
    return _home() / "hermes-os-state.json"


def _plugin_state_dir() -> Path:
    return _home() / "plugins" / "hermes-os-mission-control" / "state"


def _plugin_ui_state_path() -> Path:
    return _plugin_state_dir() / "mission-control-ui.json"


def _read_plugin_ui_state() -> Dict[str, Any]:
    return _read_json(_plugin_ui_state_path())


def _write_plugin_ui_state(data: Dict[str, Any]) -> None:
    # UI metadata only. This is not canonical conversation history.
    allowed = {
        "session_id", "session_created_at", "last_message_id", "connection_state",
        "operational_state", "last_error", "last_response_at", "updated_at",
        "runner_started_at", "runner_error", "last_voice_at",
    }
    safe = {k: v for k, v in data.items() if k in allowed}
    safe["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(_plugin_ui_state_path(), safe)


def _set_plugin_operational_state(state: str, error: Optional[str] = None) -> Dict[str, Any]:
    data = _read_plugin_ui_state()
    data["operational_state"] = state
    data["connection_state"] = "connected" if state not in {"error"} else "error"
    if error:
        data["last_error"] = error[:500]
    elif state != "error":
        data.pop("last_error", None)
    _write_plugin_ui_state(data)
    return data


def _read_text(path: Path, limit: int = 12000) -> str:
    try:
        if not path.exists() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")[:limit]
    except Exception:
        return ""


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")


def _read_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None:
        return {}
    try:
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _vault_path(state: Dict[str, Any]) -> Path:
    raw = state.get("vault_path") or str(DEFAULT_VAULT)
    try:
        return Path(str(raw))
    except Exception:
        return DEFAULT_VAULT


def _os_dir(state: Dict[str, Any] | None = None) -> Path:
    return _vault_path(state or _read_json(_state_json_path())) / "06 Hermes"


def _backup_before_runtime_write(path: Path) -> str:
    if not path.exists():
        return ""
    vault = DEFAULT_VAULT
    home = _home()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_mission_control_runtime"
    root = vault / "99 Archive" / "Hermes OS Backups" / stamp
    try:
        pstr = str(path).replace("\\", "/")
        vault_str = str(vault).replace("\\", "/").rstrip("/")
        if pstr.startswith(vault_str):
            dest = root / path.relative_to(vault)
        else:
            dest = root / "hermes-home" / path.relative_to(home)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        return str(dest).replace("\\", "/")
    except Exception:
        return ""


def _section(text: str, heading: str, limit: int = 1600) -> str:
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    match = re.search(pattern, text, flags=re.M)
    if not match:
        return ""
    start = match.end()
    nxt = re.search(r"^##\s+", text[start:], flags=re.M)
    end = start + nxt.start() if nxt else len(text)
    return text[start:end].strip()[:limit]


def _bullets(section_text: str, max_items: int = 8) -> List[str]:
    items = []
    for line in section_text.splitlines():
        s = line.strip()
        if s.startswith("- "):
            item = s[2:].strip()
            norm = item.lower().rstrip(".")
            if norm in {"none", "no pending approvals", "no open requests"} or norm.startswith("none recorded"):
                continue
            items.append(item)
    return items[:max_items]


def _note_status(vault: Path) -> Dict[str, Any]:
    base = vault / "06 Hermes"
    notes = {}
    for key, name in OS_NOTE_NAMES.items():
        path = base / name
        notes[key] = {"path": str(path).replace("\\", "/"), "exists": path.exists(), "size": path.stat().st_size if path.exists() else 0}
    return notes


def _plugins_status() -> Dict[str, Any]:
    root = _home() / "plugins"
    names = ["hermes-os-mission-control", "hermes-memory-ui", "hermes-labyrinth", "strike-freedom-cockpit"]
    return {name: (root / name / "dashboard" / "manifest.json").exists() for name in names}


def _safe_config_status() -> Dict[str, Any]:
    cfg = _read_yaml(_home() / "config.yaml")
    def dig(*keys, default=None):
        cur: Any = cfg
        for key in keys:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(key)
        return default if cur is None else cur
    return {
        "dashboard_theme": dig("dashboard", "theme", default=""),
        "approval_mode": dig("approvals", "mode", default="unknown"),
        "memory_enabled": bool(dig("memory", "memory_enabled", default=False)),
        "user_profile_enabled": bool(dig("memory", "user_profile_enabled", default=False)),
        "stt_enabled": bool(dig("stt", "enabled", default=False)),
        "stt_provider": dig("stt", "provider", default=""),
        "tts_provider": dig("tts", "provider", default=""),
        "voice_auto_tts": bool(dig("voice", "auto_tts", default=False)),
        "voice_record_key": dig("voice", "record_key", default=""),
    }


def _project_items(registry_text: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    projects = []
    for item in state.get("active_projects", []) if isinstance(state.get("active_projects"), list) else []:
        if isinstance(item, dict):
            projects.append({"name": item.get("name", "Project"), "note": item.get("note", ""), "status": "active"})
    if not projects and registry_text:
        for name in re.findall(r"^###\s+(.+)$", registry_text, flags=re.M)[:6]:
            projects.append({"name": name.strip(), "note": "", "status": "active"})
    return projects[:8]


def _system_health(vault: Path, notes: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _safe_config_status()
    plugins = _plugins_status()
    missing = [k for k, v in notes.items() if not v.get("exists")]
    return {
        "overall": "ready" if not missing and plugins.get("hermes-os-mission-control") else "needs_attention",
        "vault_exists": vault.exists(),
        "missing_os_notes": missing,
        "plugins": plugins,
        "config": cfg,
    }


def _clean_message(message: str) -> str:
    return re.sub(r"\s+", " ", (message or "").strip())[:1200]


def _redact_text(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password|credential|bearer|cookie)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    text = re.sub(r"(?i)(Authorization:\s*Bearer\s+)\S+", r"\1[redacted]", text)
    return text[:limit]


def _risk_level(description: str, command: str) -> str:
    blob = f"{description} {command}".lower()
    if any(w in blob for w in ["delete", "destructive", "rm -rf", "credential", "config", "publish", "external", "outside"]):
        return "high"
    if any(w in blob for w in ["write", "move", "install", "download", "shell", "command"]):
        return "medium"
    return "low"


def _request_type(description: str, command: str) -> str:
    blob = f"{description} {command}".lower()
    if "command" in blob or command:
        return "shell_command"
    if "write" in blob or "file" in blob:
        return "file_operation"
    if "publish" in blob or "telegram" in blob or "message" in blob:
        return "external_message"
    return "tool_action"


def _safe_scope(command: str) -> str:
    norm = (command or "").replace("\\", "/")
    scope_norm = str(APPROVED_SCOPE).replace("\\", "/").rstrip("/")
    vault_norm = str(DEFAULT_VAULT).replace("\\", "/").rstrip("/")
    home_norm = str(_home()).replace("\\", "/").rstrip("/")
    if scope_norm and scope_norm in norm:
        return "Mission Control configured workdir"
    if vault_norm and vault_norm in norm:
        return "configured memory vault"
    if home_norm and home_norm in norm:
        return "Hermes runtime"
    if re.search(r"[A-Za-z]:/", norm):
        return "local Windows path"
    return "not disclosed"


def _language_hint(text: str) -> str:
    return "fa" if re.search(r"[\u0600-\u06ff]", text or "") else "en"


def _append_active_request(task_text: str, item: str) -> str:
    if "## Active Requests" not in task_text:
        return task_text.rstrip() + "\n\n## Active Requests\n" + item + "\n"
    section_re = re.compile(r"(^## Active Requests\s*$)(.*?)(?=^##\s+|\Z)", re.M | re.S)
    m = section_re.search(task_text)
    if not m:
        return task_text.rstrip() + "\n" + item + "\n"
    body = m.group(2)
    body_lines = [ln for ln in body.splitlines() if ln.strip().lower() not in {"- none.", "- none"}]
    new_body = "\n" + item + ("\n" + "\n".join(body_lines).rstrip() if "\n".join(body_lines).strip() else "") + "\n\n"
    return task_text[:m.start(2)] + new_body + task_text[m.end(2):]


def _conversation(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    conv = state.setdefault("mission_control_conversation", [])
    if not isinstance(conv, list):
        conv = []
    state["mission_control_conversation"] = conv
    return conv


def _update_state_for_live_chat(state: Dict[str, Any], turn: Dict[str, Any]) -> Dict[str, Any]:
    state["version"] = "0.4.0"
    dash = state.setdefault("dashboard", {})
    if isinstance(dash, dict):
        dash["version"] = "V4"
        dash["interaction_mode"] = "hermes_native_session"
        dash["talk_endpoint"] = "/api/plugins/hermes-os-mission-control/talk"
    conv = _conversation(state)
    conv.insert(0, turn)
    state["mission_control_conversation"] = conv[:30]
    state["hermes_status"] = "idle"
    state["last_mission_control_message_at"] = turn["created_at"]
    state["last_mission_control_response_at"] = turn["completed_at"]
    return state


def _update_state_for_fallback(state: Dict[str, Any], request: Dict[str, Any]) -> Dict[str, Any]:
    state["version"] = "0.4.0"
    dash = state.setdefault("dashboard", {})
    if isinstance(dash, dict):
        dash["version"] = "V4"
        dash["interaction_mode"] = "fallback_task_inbox"
        dash["talk_endpoint"] = "/api/plugins/hermes-os-mission-control/talk"
    conv = _conversation(state)
    conv.insert(0, request)
    state["mission_control_conversation"] = conv[:30]
    open_requests = state.setdefault("open_requests", [])
    if isinstance(open_requests, list):
        open_requests.insert(0, {"id": request["id"], "text": request["message"], "status": "pending", "source": "mission_control"})
        state["open_requests"] = open_requests[:20]
    state["hermes_status"] = "pending_request"
    state["last_mission_control_message_at"] = request["created_at"]
    return state


def _strip_hermes_output(output: str) -> str:
    lines = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("session_id:"):
            continue
        if line.startswith("Hermes Web UI"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _extract_session_id(output: str) -> Optional[str]:
    match = re.search(r"^session_id:\s*([A-Za-z0-9_\-]+)\s*$", output or "", flags=re.M)
    return match.group(1) if match else None


def _strip_hermes_output(output: str) -> str:
    lines = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("session_id:"):
            continue
        if line.startswith("↻ Resumed session"):
            continue
        if line.startswith("Hermes Web UI"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _mission_control_session_id() -> Optional[str]:
    state = _read_plugin_ui_state()
    sid = state.get("session_id")
    return str(sid) if sid else None


def _ensure_hermes_core_imports() -> None:
    os.environ["HERMES_HOME"] = str(_home())
    # These flags are required so Hermes command guards use the native gateway
    # approval path instead of the local non-interactive auto-allow path.
    # HERMES_EXEC_ASK is the critical switch used by terminal/code guards to
    # block the agent thread until Mission Control resolves approve/deny.
    os.environ["HERMES_GATEWAY_SESSION"] = "1"
    os.environ["HERMES_EXEC_ASK"] = "1"
    core = str(HERMES_CORE)
    if core not in sys.path:
        sys.path.insert(0, core)


class MissionControlRunner:
    """One trusted in-process, gateway-like Hermes runner for Mission Control."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._turn_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mission-control-hermes")
        self._cli = None
        self._session_id: Optional[str] = None
        self._approvals: Dict[str, Dict[str, Any]] = {}
        self._started_at = datetime.now().isoformat(timespec="seconds")
        self._last_error: Optional[str] = None
        self._active_future = None

    @property
    def session_id(self) -> Optional[str]:
        with self._lock:
            return self._session_id

    def startup(self, force_new: bool = False) -> None:
        with self._lock:
            if self._cli is not None and not force_new:
                return
            _ensure_hermes_core_imports()
            from cli import HermesCLI
            prior = None if force_new else _mission_control_session_id()
            self._cli = HermesCLI(resume=prior, verbose=False, compact=True, pass_session_id=True)
            self._session_id = str(getattr(self._cli, "session_id", "") or prior or "")
            if not self._session_id:
                raise RuntimeError("Hermes did not provide a native session ID.")
            self._register_approval_callback_locked()
            data = _read_plugin_ui_state()
            data["session_id"] = self._session_id
            data.setdefault("session_created_at", datetime.now().isoformat(timespec="seconds"))
            data["runner_started_at"] = self._started_at
            data["connection_state"] = "connected"
            data["operational_state"] = data.get("operational_state") or "idle"
            data.pop("runner_error", None)
            _write_plugin_ui_state(data)

    def shutdown(self) -> None:
        with self._lock:
            if self._session_id:
                try:
                    from tools.approval import unregister_gateway_notify
                    unregister_gateway_notify(self._session_id)
                except Exception:
                    pass
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._cli = None

    def _register_approval_callback_locked(self) -> None:
        if not self._session_id:
            return
        from tools.approval import register_gateway_notify
        register_gateway_notify(self._session_id, self._notify_approval)

    def _sync_session_locked(self) -> None:
        if self._cli is None:
            return
        sid = str(getattr(self._cli, "session_id", "") or "")
        if sid and sid != self._session_id:
            if self._session_id:
                try:
                    from tools.approval import unregister_gateway_notify
                    unregister_gateway_notify(self._session_id)
                except Exception:
                    pass
            self._session_id = sid
            self._register_approval_callback_locked()
        if self._session_id:
            data = _read_plugin_ui_state()
            data["session_id"] = self._session_id
            data.setdefault("session_created_at", datetime.now().isoformat(timespec="seconds"))
            _write_plugin_ui_state(data)

    def _notify_approval(self, approval_data: Dict[str, Any]) -> None:
        with self._lock:
            sid = self._session_id or _mission_control_session_id() or "mission-control"
            command = str(approval_data.get("command") or "")
            description = str(approval_data.get("description") or "Approval required")
            approval_id = f"{sid}:{uuid.uuid4().hex[:12]}"
            created = datetime.now().isoformat(timespec="seconds")
            record = {
                "id": approval_id,
                "session_id": sid,
                "safe_summary": _redact_text(description, 240),
                "request_type": _request_type(description, command),
                "requesting_agent": "Hermes native runner",
                "safe_command_preview": _redact_text(command, 240),
                "target_scope": _safe_scope(command),
                "risk_level": _risk_level(description, command),
                "creation_time": created,
                "status": "pending",
            }
            self._approvals[approval_id] = record
            _set_plugin_operational_state("approval_waiting")

    def approvals(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted((dict(v) for v in self._approvals.values()), key=lambda r: r.get("creation_time", ""), reverse=True)[:20]

    def resolve(self, approval_id: str, approve: bool) -> Dict[str, Any]:
        with self._lock:
            record = self._approvals.get(approval_id)
            if not record:
                return {"ok": False, "state": _runtime_state(_read_json(_state_json_path()), _tool_services(_vault_path(_read_json(_state_json_path())))), "error": "Approval ID is not pending in this Mission Control session."}
            if record.get("session_id") != self._session_id:
                return {"ok": False, "state": "error", "error": "Approval/session mismatch. Cross-session approval resolution refused."}
            if record.get("status") != "pending":
                return {"ok": False, "state": "approval_waiting", "status": record.get("status"), "error": "Approval was already resolved."}
            pending = [r for r in sorted(self._approvals.values(), key=lambda r: r.get("creation_time", "")) if r.get("status") == "pending"]
            if pending and pending[0].get("id") != approval_id:
                return {"ok": False, "state": "approval_waiting", "error": "Hermes native approval resolver is FIFO; approve or deny the oldest pending request first."}
            choice = "once" if approve else "deny"
            from tools.approval import resolve_gateway_approval
            resolved = resolve_gateway_approval(str(self._session_id), choice, resolve_all=False)
            if resolved < 1:
                record["status"] = "expired"
                return {"ok": False, "state": "approval_waiting", "status": "expired", "error": "Native approval expired or is no longer waiting."}
            record["status"] = "approved" if approve else "denied"
            record["resolved_at"] = datetime.now().isoformat(timespec="seconds")
            _set_plugin_operational_state("working")
            return {"ok": True, "state": "working", "status": record["status"], "approval": dict(record)}

    def run_turn(self, message: str, force_new: bool = False, source: str = "typed") -> Dict[str, Any]:
        if not self._turn_lock.acquire(blocking=False):
            return {"ok": False, "state": "working", "error": "Mission Control already has an active Hermes turn. Duplicate submission refused."}
        try:
            self.startup(force_new=force_new)
            _set_plugin_operational_state("thinking")
            future = self._executor.submit(self._run_turn_sync, message, source)
            with self._lock:
                self._active_future = future
            try:
                return future.result(timeout=IN_PROCESS_RUNNER_TIMEOUT_SECONDS)
            except FutureTimeout:
                with self._lock:
                    if self._cli is not None and getattr(self._cli, "agent", None) is not None:
                        try:
                            self._cli.agent.interrupt("Mission Control runner timeout")
                        except Exception:
                            pass
                _set_plugin_operational_state("error", "Hermes in-process runner timed out.")
                return {"ok": False, "state": "error", "session_id": self.session_id, "error": f"Hermes in-process runner timed out after {IN_PROCESS_RUNNER_TIMEOUT_SECONDS}s."}
        finally:
            with self._lock:
                self._active_future = None
            self._turn_lock.release()

    def _run_turn_sync(self, message: str, source: str) -> Dict[str, Any]:
        with self._lock:
            cli = self._cli
        if cli is None:
            raise RuntimeError("Mission Control runner was not initialized.")
        try:
            with self._lock:
                sid_for_context = self._session_id or str(getattr(cli, "session_id", "") or "mission-control")
            from gateway.session_context import set_session_vars, clear_session_vars
            from tools.approval import set_current_session_key, reset_current_session_key
            session_tokens = set_session_vars(
                platform="mission-control",
                chat_id="mission-control",
                chat_name="Mission Control",
                user_id="ashki",
                user_name="Ashki",
                session_key=sid_for_context,
                cwd=str(APPROVED_SCOPE),
            )
            approval_token = set_current_session_key(sid_for_context)
            os.environ["HERMES_SESSION_KEY"] = sid_for_context
            os.environ["HERMES_SESSION_PLATFORM"] = "mission-control"
            try:
                # Keep CLI/rich output and tool progress out of HTTP responses/logs.
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    response = cli.chat(message)
            finally:
                reset_current_session_key(approval_token)
                clear_session_vars(session_tokens)
            with self._lock:
                self._sync_session_locked()
                sid = self._session_id
            if not response:
                _set_plugin_operational_state("error", "Hermes returned an empty response.")
                return {"ok": False, "state": "error", "session_id": sid, "error": "Hermes returned an empty response."}
            data = _read_plugin_ui_state()
            if sid:
                data["session_id"] = sid
            data["connection_state"] = "connected"
            data["operational_state"] = "idle"
            data["last_response_at"] = datetime.now().isoformat(timespec="seconds")
            data.pop("last_error", None)
            data.pop("runner_error", None)
            _write_plugin_ui_state(data)
            return {
                "ok": True,
                "state": "speaking" if source == "voice" else "idle",
                "status": "answered",
                "mode": "hermes_in_process_native_session",
                "session_id": sid,
                "response": response,
                "reply": response,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        except Exception as exc:
            err = _redact_text(exc, 500)
            with self._lock:
                self._last_error = err
                self._sync_session_locked()
                sid = self._session_id
            _set_plugin_operational_state("error", err)
            return {"ok": False, "state": "error", "session_id": sid, "error": err}


_RUNNER: Optional[MissionControlRunner] = None
_RUNNER_LOCK = threading.Lock()


def _runner() -> MissionControlRunner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = MissionControlRunner()
        return _RUNNER


def _run_native_hermes_turn(message: str, force_new: bool = False, source: str = "typed") -> Dict[str, Any]:
    """Run one Mission Control turn through the shared in-process Hermes runner."""
    return _runner().run_turn(message, force_new=force_new, source=source)


def _run_native_hermes_turn_subprocess_rollback(message: str, force_new: bool = False) -> Dict[str, Any]:
    """Rollback-only copy of the old Phase 2 subprocess bridge. Not used automatically."""
    if not shutil.which("hermes"):
        return {"ok": False, "state": "error", "error": "Hermes CLI is not available on PATH."}
    ui_state = _read_plugin_ui_state()
    session_id = None if force_new else ui_state.get("session_id")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(_home())
    cmd = ["hermes", "chat"]
    if session_id:
        cmd += ["--resume", str(session_id)]
    cmd += ["-q", message, "-Q", "--source", "mission-control", "--pass-session-id"]
    _set_plugin_operational_state("thinking")
    try:
        proc = subprocess.run(cmd, cwd=str(_home()), env=env, text=True, capture_output=True, timeout=LIVE_BRIDGE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _set_plugin_operational_state("error", "Hermes native session bridge timed out.")
        return {"ok": False, "state": "error", "session_id": session_id, "error": f"Hermes native session bridge timed out after {LIVE_BRIDGE_TIMEOUT_SECONDS}s."}
    except Exception as exc:
        _set_plugin_operational_state("error", str(exc))
        return {"ok": False, "state": "error", "session_id": session_id, "error": f"Hermes native session bridge failed to start: {exc}"}
    raw = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    new_session_id = _extract_session_id(raw) or session_id
    output = _strip_hermes_output(raw)
    if proc.returncode != 0:
        _set_plugin_operational_state("error", output or f"Hermes exited with code {proc.returncode}.")
        return {"ok": False, "state": "error", "session_id": new_session_id, "error": output or f"Hermes exited with code {proc.returncode}."}
    return {"ok": True, "state": "speaking", "status": "answered", "mode": "rollback_subprocess_bridge", "session_id": new_session_id, "response": output, "reply": output, "created_at": datetime.now().isoformat(timespec="seconds")}


def _export_native_session(session_id: str) -> Dict[str, Any]:
    if not session_id or not shutil.which("hermes"):
        return {"ok": False, "error": "No Hermes session is available."}
    try:
        proc = subprocess.run(
            ["hermes", "sessions", "export", "-", "--session-id", session_id],
            cwd=str(_home()),
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "error": ((proc.stdout or "") + (proc.stderr or "")).strip()[:500]}
    try:
        exported = json.loads(proc.stdout)
    except Exception as exc:
        return {"ok": False, "error": f"Could not parse Hermes session export: {exc}"}
    messages = []
    for msg in exported.get("messages", []) if isinstance(exported, dict) else []:
        if msg.get("role") not in {"user", "assistant"}:
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        messages.append({
            "id": msg.get("id"),
            "role": msg.get("role"),
            "content": content,
            "timestamp": msg.get("timestamp"),
        })
    return {
        "ok": True,
        "session_id": session_id,
        "source": "hermes sessions export",
        "messages": messages[-40:],
    }


# ---- Phase 1 read-only runtime snapshot helpers ---------------------------
_CACHE: Dict[str, Dict[str, Any]] = {}
SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|credential|auth|bearer|cookie|private)", re.I)


def _cache_get(key: str, ttl: float, fn):
    now = time.time()
    entry = _CACHE.get(key)
    if entry and now - float(entry.get("at", 0)) < ttl:
        return entry.get("value")
    value = fn()
    _CACHE[key] = {"at": now, "value": value}
    return value


def _redact_config_value(key: str, value: Any) -> Any:
    if SENSITIVE_KEY_RE.search(str(key)):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: _redact_config_value(k, v) for k, v in value.items() if not SENSITIVE_KEY_RE.search(str(k))}
    if isinstance(value, list):
        return [_redact_config_value(key, v) for v in value[:10]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(type(value).__name__)


def _safe_model_metadata() -> Dict[str, Any]:
    cfg = _read_yaml(_home() / "config.yaml")
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    provider = None
    model = None
    if isinstance(model_cfg, dict):
        provider = model_cfg.get("provider") or model_cfg.get("default_provider")
        model = model_cfg.get("default") or model_cfg.get("model") or model_cfg.get("name")
    # Some installs keep provider/model at top level or under providers/default.
    provider = provider or cfg.get("provider") if isinstance(cfg, dict) else provider
    model = model or cfg.get("model") if isinstance(cfg.get("model"), str) else model
    return {
        "provider": str(provider) if provider else None,
        "model": str(model) if model else None,
        "source": "config.yaml" if (provider or model) else "unavailable",
    }


def _command_available(command: str) -> Dict[str, Any]:
    path = shutil.which(command)
    return {"available": bool(path), "path": path or None}


def _run_quick_command(cmd: List[str], timeout: float = 3.0) -> Dict[str, Any]:
    try:
        proc = subprocess.run(cmd, cwd=str(_home()), text=True, capture_output=True, timeout=timeout)
        output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        output = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+", r"\1=[redacted]", output)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output[:1200]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "output": "timeout"}
    except Exception as exc:
        return {"ok": False, "returncode": None, "output": str(exc)[:300]}


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _dashboard_available() -> Dict[str, Any]:
    return {"available": _port_open("127.0.0.1", 9119), "url": "http://127.0.0.1:9119"}


def _parse_float(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        if not text or text.upper() in {"N/A", "NA", "[N/A]", "NONE"}:
            return None
        return float(text)
    except Exception:
        return None


def _parse_int(value: Any) -> Optional[int]:
    parsed = _parse_float(value)
    return int(round(parsed)) if parsed is not None else None


def _first_gpu_row() -> Dict[str, Any]:
    base = {
        "name": None,
        "utilization_percent": None,
        "temperature_c": None,
        "vram_used_mb": None,
        "vram_total_mb": None,
        "power_watts": None,
        "fan_percent": None,
        "status": "unavailable",
        "source": "nvidia-smi",
    }
    if not shutil.which("nvidia-smi"):
        return {**base, "source": "nvidia-smi missing"}
    result = _run_quick_command([
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw,fan.speed",
        "--format=csv,noheader,nounits",
    ], timeout=2.0)
    if not result.get("ok"):
        return {**base, "source": "nvidia-smi", "error": _redact_text(result.get("output"), 180)}
    line = (result.get("output") or "").splitlines()[0:1]
    if not line:
        return {**base, "source": "nvidia-smi empty"}
    parts = [part.strip() for part in line[0].split(",")]
    while len(parts) < 7:
        parts.append("")
    return {
        "name": parts[0] or None,
        "utilization_percent": _parse_float(parts[1]),
        "temperature_c": _parse_float(parts[2]),
        "vram_used_mb": _parse_int(parts[3]),
        "vram_total_mb": _parse_int(parts[4]),
        "power_watts": _parse_float(parts[5]),
        "fan_percent": _parse_float(parts[6]),
        "status": "available",
        "source": "nvidia-smi",
    }


def _psutil_hardware() -> Dict[str, Any]:
    cpu = {"utilization_percent": None, "frequency_mhz": None, "temperature_c": None, "status": "unavailable", "source": "psutil"}
    memory = {"used_bytes": None, "total_bytes": None, "utilization_percent": None, "status": "unavailable", "source": "psutil"}
    hermes_processes = {"count": None, "status": "unavailable", "source": "psutil process names only"}
    try:
        import psutil  # type: ignore
    except Exception:
        return cpu, memory, hermes_processes
    try:
        cpu["utilization_percent"] = float(psutil.cpu_percent(interval=0.05))
        freq = psutil.cpu_freq()
        cpu["frequency_mhz"] = float(freq.current) if freq and freq.current else None
        # psutil temperature support is not reliable on Windows; only return a value if
        # psutil exposes one locally. Otherwise be explicit and honest.
        try:
            temps = psutil.sensors_temperatures(fahrenheit=False) if hasattr(psutil, "sensors_temperatures") else {}
        except Exception:
            temps = {}
        temp_values = []
        for entries in (temps or {}).values():
            for entry in entries or []:
                current = getattr(entry, "current", None)
                if isinstance(current, (int, float)) and -20 <= current <= 125:
                    temp_values.append(float(current))
        cpu["temperature_c"] = round(sum(temp_values) / len(temp_values), 1) if temp_values else None
        cpu["status"] = "available"
    except Exception as exc:
        cpu["error"] = _redact_text(exc, 160)
    try:
        vm = psutil.virtual_memory()
        memory.update({
            "used_bytes": int(vm.used),
            "total_bytes": int(vm.total),
            "utilization_percent": float(vm.percent),
            "status": "available",
        })
    except Exception as exc:
        memory["error"] = _redact_text(exc, 160)
    try:
        count = 0
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if "hermes" in name or name in {"python.exe", "python"}:
                # Count Hermes-named processes plus the Python-hosted dashboard/plugin process
                # without exposing argv, environment, cwd, or credentials.
                if "hermes" in name:
                    count += 1
        hermes_processes.update({"count": count, "status": "available"})
    except Exception as exc:
        hermes_processes["error"] = _redact_text(exc, 160)
    return cpu, memory, hermes_processes


def _hardware_telemetry() -> Dict[str, Any]:
    def check():
        cpu, memory, hermes_processes = _psutil_hardware()
        return {
            "gpu": _first_gpu_row(),
            "cpu": cpu,
            "memory": memory,
            "hermes_processes": hermes_processes,
            "polling_hint_seconds": {"gpu_cpu_memory": 2, "expensive_sensors": 10},
        }
    return _cache_get("hardware_telemetry", 2.0, check)


def _gateway_status() -> Dict[str, Any]:
    def check():
        if not shutil.which("hermes"):
            return {"available": False, "status": "unavailable", "source": "hermes_cli_missing"}
        result = _run_quick_command(["hermes", "gateway", "status"], timeout=3.0)
        raw = result.get("output") or ""
        low = raw.lower()
        if result.get("ok") and any(word in low for word in ["running", "active", "connected"]):
            status = "running"
        elif result.get("ok"):
            status = "available"
        elif "not running" in low or "stopped" in low:
            status = "stopped"
        else:
            status = "unknown"
        return {"available": status in {"running", "available"}, "status": status, "source": "hermes gateway status"}
    return _cache_get("gateway_status", 30, check)


def _hermes_status() -> Dict[str, Any]:
    def check():
        cli = _command_available("hermes")
        if not cli["available"]:
            return {"available": False, "status": "unavailable", "source": "PATH"}
        result = _run_quick_command(["hermes", "status", "--all"], timeout=4.0)
        status = "ready" if result.get("ok") else "unknown"
        return {"available": True, "status": status, "source": "hermes status --all", "last_check_ok": bool(result.get("ok"))}
    return _cache_get("hermes_status", 30, check)


def _tool_services(vault: Path) -> Dict[str, Any]:
    whisper_dir = _whisper_dir()
    piper_dir = _piper_dir()
    services = {
        "dashboard": {**_dashboard_available(), "status": "online" if _dashboard_available()["available"] else "offline"},
        "plugin": {"available": True, "status": "online", "version": PLUGIN_VERSION},
        "hermes_cli": {**_command_available("hermes")},
        "gateway": _gateway_status(),
        "memory_vault": {"available": vault.exists(), "status": "available" if vault.exists() else "unavailable", "path": str(vault).replace("\\", "/") if vault.exists() else None},
        "claude_code_cli": {**_command_available("claude")},
        "whisper": {"available": bool(shutil.which("whisper") or whisper_dir.exists()), "command": shutil.which("whisper"), "config_dir_exists": whisper_dir.exists()},
        "piper": {"available": bool(shutil.which("piper") or piper_dir.exists()), "command": shutil.which("piper"), "config_dir_exists": piper_dir.exists()},
    }
    for key, value in services.items():
        if "status" not in value:
            value["status"] = "available" if value.get("available") else "unavailable"
    return services


def _runtime_state(state: Dict[str, Any], services: Dict[str, Any]) -> str:
    ui = _read_plugin_ui_state()
    raw = ui.get("operational_state") or state.get("runtime_state") or state.get("hermes_status")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if not services.get("hermes_cli", {}).get("available"):
        return "offline"
    if not services.get("dashboard", {}).get("available"):
        return "degraded"
    return "idle"


def _active_task(state: Dict[str, Any]) -> Any:
    for key in ["active_task", "current_task", "current_focus"]:
        val = state.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:500]
        if isinstance(val, dict):
            return _redact_config_value(key, val)
    conv = state.get("mission_control_conversation")
    if isinstance(conv, list) and conv:
        first = conv[0] if isinstance(conv[0], dict) else {}
        if first.get("status") in {"pending", "running", "working"}:
            return {"id": first.get("id"), "status": first.get("status"), "source": first.get("source")}
    return None


def _last_response_timestamp(state: Dict[str, Any]) -> Any:
    ui = _read_plugin_ui_state()
    if ui.get("last_response_at"):
        return ui.get("last_response_at")
    for key in ["last_mission_control_response_at", "last_response_at", "updated_at"]:
        if state.get(key):
            return state.get(key)
    return None


def _runtime_payload() -> Dict[str, Any]:
    state = _read_json(_state_json_path())
    vault = _vault_path(state)
    services = _tool_services(vault)
    hermes = _hermes_status()
    runtime_state = _runtime_state(state, services)
    model = _safe_model_metadata()
    active_task = _active_task(state)
    last_response_at = _last_response_timestamp(state)
    return {
        "version": PLUGIN_VERSION,
        "generated_at": time.time(),
        "runtime_state": runtime_state,
        "hermes": {
            "availability": "available" if hermes.get("available") else "unavailable",
            "status": hermes.get("status") or "unknown",
            "active_task": active_task,
            "source": hermes.get("source"),
        },
        "model": model,
        "health": {
            "dashboard": services["dashboard"]["status"],
            "plugin": services["plugin"]["status"],
            "hermes_cli": services["hermes_cli"]["status"],
            "gateway": services["gateway"].get("status", "unknown"),
            "memory_vault": services["memory_vault"]["status"],
            "claude_code_cli": services["claude_code_cli"]["status"],
            "whisper": services["whisper"]["status"],
            "piper": services["piper"]["status"],
        },
        "services": services,
        "hardware": _hardware_telemetry(),
        "telemetry": {
            "uptime_seconds": None,
            "last_response_at": last_response_at,
            "active_processes": None,
            "source": "state file, plugin UI metadata, and cached service probes",
            "mission_control_session_id": _mission_control_session_id(),
        },
    }

@router.get("/runtime")
def runtime() -> Dict[str, Any]:
    """Read-only Hermes runtime state for Mission Control Phase 1."""
    return _runtime_payload()


@router.get("/services")
def services() -> Dict[str, Any]:
    """Read-only dependency/service availability map."""
    state = _read_json(_state_json_path())
    vault = _vault_path(state)
    return {"ok": True, "version": PLUGIN_VERSION, "generated_at": time.time(), "services": _tool_services(vault), "hardware": _hardware_telemetry()}


@router.get("/snapshot")
def snapshot() -> Dict[str, Any]:
    state = _read_json(_state_json_path())
    vault = _vault_path(state)
    os_dir = vault / "06 Hermes"
    notes = _note_status(vault)

    state_text = _read_text(os_dir / OS_NOTE_NAMES["state"])
    focus_text = _read_text(os_dir / OS_NOTE_NAMES["focus"])
    task_text = _read_text(os_dir / OS_NOTE_NAMES["task_inbox"])
    approval_text = _read_text(os_dir / OS_NOTE_NAMES["approvals"])
    memory_text = _read_text(os_dir / OS_NOTE_NAMES["memory_routing"])
    registry_text = _read_text(os_dir / OS_NOTE_NAMES["project_registry"])
    mission_text = _read_text(os_dir / OS_NOTE_NAMES["mission_control"])

    cfg = _safe_config_status()
    active_requests = _bullets(_section(task_text, "Active Requests"), 12)
    pending_approvals = _bullets(_section(state_text, "Pending Approvals"), 8)
    runtime_payload = _runtime_payload()

    payload = {
        "version": PLUGIN_VERSION,
        "generated_at": runtime_payload["generated_at"],
        "runtime_state": runtime_payload["runtime_state"],
        "hermes": runtime_payload["hermes"],
        "model": runtime_payload["model"],
        "health": runtime_payload["health"],
        "services": runtime_payload["services"],
        "hardware": runtime_payload.get("hardware", {}),
        "telemetry": runtime_payload["telemetry"],
        # Existing V4 fields retained for UI compatibility.
        "state": state,
        "hermes_status": runtime_payload["hermes"].get("status") or state.get("hermes_status") or "unknown",
        "interaction_mode": (state.get("dashboard") or {}).get("interaction_mode") or "hermes_in_process_native_session",
        "conversation": _export_native_session(_mission_control_session_id()).get("messages", []) if _mission_control_session_id() else [],
        "current_focus": {
            "summary": state.get("current_focus") or "",
            "note_section": _section(focus_text, "Current Focus", 900),
            "mode": _section(focus_text, "Active Mode", 700),
        },
        "active_projects": _project_items(registry_text, state),
        "open_requests": active_requests,
        "pending_approvals": pending_approvals,
        "memory_health": {
            "routes": _bullets(_section(memory_text, "Routes"), 10),
            "memory_enabled": cfg["memory_enabled"],
            "user_profile_enabled": cfg["user_profile_enabled"],
            "vault_path": str(vault).replace("\\", "/") if vault.exists() else None,
            "availability": runtime_payload["services"]["memory_vault"]["status"],
        },
        "vault_health": {
            "vault_exists": vault.exists(),
            "os_dir_exists": os_dir.exists(),
            "project_dir_exists": (vault / "02 Projects").exists(),
            "notes": notes,
        },
        "system_health": _system_health(vault, notes),
        "voice_status": {
            "stt_enabled": cfg["stt_enabled"],
            "stt_provider": cfg["stt_provider"],
            "tts_provider": cfg["tts_provider"],
            "auto_tts": cfg["voice_auto_tts"],
            "record_key": cfg["voice_record_key"],
            "connection_state": "connected_phase_3",
        },
        "mission_control": {
            "panels": _bullets(_section(mission_text, "Core Panels"), 12),
            "links": [
                {"label": "Memory UI", "path": "/memory"},
                {"label": "Labyrinth", "path": "/labyrinth"},
                {"label": "Hermes OS State", "path": str(os_dir / OS_NOTE_NAMES["state"]).replace("\\", "/")},
            ],
        },
        "approval_workflow": {
            "levels": _section(approval_text, "Levels", 1200),
            "default_behavior": _section(approval_text, "Default Behavior", 900),
            "connection_state": "connected_phase_3",
        },
    }
    return payload



def _whisper_dir() -> Path:
    candidates = [
        os.environ.get("HERMES_MISSION_CONTROL_WHISPER_DIR"),
        str(Path.home() / ".config" / "whisper"),
    ]
    for raw in candidates:
        if not raw:
            continue
        p = Path(raw)
        if p.exists():
            return p
    return Path(candidates[-1])


def _piper_dir() -> Path:
    candidates = [
        os.environ.get("HERMES_MISSION_CONTROL_PIPER_DIR"),
        str(Path.home() / ".config" / "piper"),
    ]
    for raw in candidates:
        if not raw:
            continue
        p = Path(raw)
        if p.exists():
            return p
    return Path(candidates[-1])


def _transcribe_audio_bytes(audio: bytes, language: str = "auto") -> Dict[str, Any]:
    wdir = _whisper_dir()
    exe = wdir / "whisper-cli.exe"
    model = wdir / "ggml-large-v3-turbo.bin"
    ffmpeg = shutil.which("ffmpeg")
    if not exe.exists() or not model.exists():
        return {"ok": False, "error": "Local Whisper executable/model is unavailable."}
    if not ffmpeg:
        return {"ok": False, "error": "ffmpeg is unavailable for browser audio conversion."}
    with tempfile.TemporaryDirectory(prefix="mc-voice-") as td:
        raw = Path(td) / "input.webm"
        wav = Path(td) / "input.wav"
        raw.write_bytes(audio)
        conv = subprocess.run([ffmpeg, "-y", "-i", str(raw), "-ar", "16000", "-ac", "1", str(wav)], text=True, capture_output=True, timeout=45)
        if conv.returncode != 0:
            return {"ok": False, "error": _redact_text((conv.stderr or conv.stdout), 400)}
        lang = language if language in {"auto", "en", "fa"} else "auto"

        def run_whisper(lang_code: str):
            proc = subprocess.run([str(exe), "-m", str(model), "-f", str(wav), "-l", lang_code, "-nt", "-np"], text=True, capture_output=True, timeout=120)
            out = (proc.stdout or "").strip()
            if not out:
                # whisper.cpp sometimes writes useful text mixed with diagnostics; keep only plausible transcript lines.
                lines = []
                for line in (proc.stderr or "").splitlines():
                    t = line.strip()
                    if t and not any(t.lower().startswith(prefix) for prefix in ["whisper_", "ggml_", "main:", "system_info", "error:"]):
                        lines.append(t)
                out = "\n".join(lines).strip()
            return proc, _clean_message(out)

        proc, text = run_whisper(lang)
        # Auto-detect occasionally misclassifies short Persian clips. If it emits
        # obvious non-Persian/non-English script noise, retry with Persian fixed.
        if lang == "auto" and (re.search(r"[\u0f00-\u0fff]", text or "") or re.search(r"\b(salam|jarvis|hale)\b", text or "", re.I)):
            proc_fa, text_fa = run_whisper("fa")
            if text_fa:
                proc, text, lang = proc_fa, text_fa, "fa"
        if proc.returncode != 0 and not text:
            return {"ok": False, "error": _redact_text(proc.stderr or proc.stdout, 500)}
        return {"ok": bool(text), "text": text, "language": lang, "error": "No speech detected." if not text else None}


def _speak_final_response(text: str, language: str = "auto") -> Dict[str, Any]:
    pdir = _piper_dir()
    exe = pdir / "piper.exe"
    if not exe.exists():
        return {"ok": False, "error": "Local Piper executable is unavailable."}
    lang = language if language in {"en", "fa"} else _language_hint(text)
    model = pdir / ("fa_IR-gyro-medium.onnx" if lang == "fa" else "en_US-amy-medium.onnx")
    if not model.exists():
        return {"ok": False, "error": f"Piper voice model unavailable for {lang}."}
    with tempfile.TemporaryDirectory(prefix="mc-tts-") as td:
        wav = Path(td) / "reply.wav"
        proc = subprocess.run([str(exe), "-m", str(model), "-f", str(wav), "-q"], input=text[:2000], text=True, capture_output=True, timeout=90)
        if proc.returncode != 0 or not wav.exists():
            return {"ok": False, "error": _redact_text(proc.stderr or proc.stdout, 500)}
        data = base64.b64encode(wav.read_bytes()).decode("ascii")
        return {"ok": True, "language": lang, "mime": "audio/wav", "audio_base64": data}

@router.post("/talk")
def talk(body: TalkBody) -> Dict[str, Any]:
    message = _clean_message(body.message)
    if not message:
        return {"ok": False, "mode": "hermes_in_process_native_session", "status": "empty", "state": "listening", "error": "Message is empty."}
    result = _run_native_hermes_turn(message, force_new=bool(body.new_conversation), source="typed")
    result["message"] = message
    result["mode"] = "hermes_in_process_native_session"
    if result.get("session_id"):
        result["conversation"] = _export_native_session(str(result["session_id"])).get("messages", [])
    return result


@router.post("/voice")
async def voice(request: Request, muted: bool = False, language: str = "auto") -> Dict[str, Any]:
    audio = await request.body()
    if not audio:
        return {"ok": False, "state": "listening", "error": "No audio received."}
    _set_plugin_operational_state("listening")
    tx = _transcribe_audio_bytes(audio, language=language)
    if not tx.get("ok"):
        _set_plugin_operational_state("error", tx.get("error"))
        return {"ok": False, "state": "error", "transcription": tx, "error": tx.get("error") or "Transcription failed."}
    data = _read_plugin_ui_state()
    data["last_voice_at"] = datetime.now().isoformat(timespec="seconds")
    _write_plugin_ui_state(data)
    result = _run_native_hermes_turn(str(tx["text"]), force_new=False, source="voice")
    result["transcript"] = tx["text"]
    result["transcription"] = tx
    result["mode"] = "hermes_in_process_native_session"
    if result.get("session_id"):
        result["conversation"] = _export_native_session(str(result["session_id"])).get("messages", [])
    if result.get("ok") and not muted:
        result["tts"] = _speak_final_response(str(result.get("reply") or result.get("response") or ""), language=tx.get("language") or "auto")
    else:
        result["tts"] = {"ok": False, "muted": bool(muted)}
    return result


@router.post("/tts")
def tts(body: TalkBody) -> Dict[str, Any]:
    text = _clean_message(body.message)
    if not text:
        return {"ok": False, "error": "No text to speak."}
    return _speak_final_response(text, language="auto")


@router.get("/approvals")
def approvals() -> Dict[str, Any]:
    items = _runner().approvals()
    state = "approval_waiting" if any(x.get("status") == "pending" for x in items) else (_read_plugin_ui_state().get("operational_state") or "idle")
    return {"ok": True, "mode": "hermes_native_approvals", "session_id": _runner().session_id, "state": state, "approvals": items}


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: str) -> Dict[str, Any]:
    return _runner().resolve(approval_id, approve=True)


@router.post("/approvals/{approval_id}/deny")
def deny(approval_id: str) -> Dict[str, Any]:
    return _runner().resolve(approval_id, approve=False)


@router.get("/conversation")
def conversation() -> Dict[str, Any]:
    ui = _read_plugin_ui_state()
    session_id = ui.get("session_id")
    if not session_id:
        return {"ok": True, "mode": "hermes_in_process_native_session", "session_id": None, "state": ui.get("operational_state") or "listening", "messages": []}
    exported = _export_native_session(str(session_id))
    if not exported.get("ok"):
        return {"ok": False, "mode": "hermes_in_process_native_session", "session_id": session_id, "state": ui.get("operational_state") or "error", "error": exported.get("error")}
    exported["mode"] = "hermes_in_process_native_session"
    exported["state"] = ui.get("operational_state") or "listening"
    exported["connection_state"] = ui.get("connection_state") or "connected"
    return exported


@router.get("/health")
def health() -> Dict[str, Any]:
    runtime_payload = _runtime_payload()
    ok = runtime_payload["services"].get("plugin", {}).get("available", False)
    return {
        "ok": bool(ok),
        "version": PLUGIN_VERSION,
        "generated_at": runtime_payload["generated_at"],
        "runtime_state": runtime_payload["runtime_state"],
        "hermes": runtime_payload["hermes"],
        "health": runtime_payload["health"],
        "interaction_mode": "hermes_in_process_native_session",
    }
