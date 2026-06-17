# Mission Control OS for Hermes Agent

A local-first Mission Control dashboard plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It adds a `/mission-control` cockpit tab with runtime status, memory/vault awareness, hardware telemetry, conversation view, Talk-to-Hermes bridge, approvals panel, browser voice, and local TTS hooks.

> Status: early public release. The dashboard shell and plugin API are functional, but this is still a power-user/local-first plugin. Sensitive actions remain approval-gated by Hermes.

## What it adds

- Mission Control dashboard tab at `/mission-control`
- FastAPI plugin backend mounted under `/api/plugins/hermes-os-mission-control/`
- Runtime/service snapshot: Hermes CLI, gateway, vault, dashboard, model/provider, GPU/CPU/RAM
- Native Hermes session-backed Talk endpoint
- Native approval queue bridge
- Conversation export from Hermes session storage
- Browser voice endpoint using local Whisper assets when configured
- TTS endpoint using local Piper assets when configured

## Requirements

- Hermes Agent installed and working
- Hermes dashboard enabled/running
- Python dependencies already available in Hermes runtime (`fastapi`, `pydantic`, optional `yaml`, optional `psutil`)
- Optional for voice: `ffmpeg`, Whisper.cpp assets, Piper assets

## Install from GitHub

```powershell
hermes plugins install MrAshki/mission-control-os --enable
hermes dashboard --no-open
```

Then open:

```text
http://127.0.0.1:9119/mission-control
```

If the dashboard was already running, restart it after install so the Python API is imported.

## Manual clone install

```powershell
git clone https://github.com/MrAshki/mission-control-os.git
cd mission-control-os
hermes plugins install . --enable
hermes dashboard --no-open
```

## Configuration

The plugin avoids hardcoded user paths. Configure these environment variables only if auto-detection is not enough:

| Variable | Purpose | Default |
| --- | --- | --- |
| `HERMES_HOME` | Hermes profile/runtime home | Hermes default |
| `HERMES_AGENT_CORE` | Hermes source/runtime directory for in-process imports | `$HERMES_HOME/hermes-agent` |
| `HERMES_MISSION_CONTROL_VAULT` | Memory/Obsidian vault root | `OBSIDIAN_VAULT_PATH`, `HERMES_VAULT_PATH`, then `~/Memory` |
| `HERMES_MISSION_CONTROL_WORKDIR` | Safe working directory for Mission Control Talk turns | configured vault root |
| `HERMES_MISSION_CONTROL_WHISPER_DIR` | Whisper.cpp executable/model directory | `~/.config/whisper` |
| `HERMES_MISSION_CONTROL_PIPER_DIR` | Piper executable/model directory | `~/.config/piper` |

## API endpoints

Hermes mounts the plugin backend at `/api/plugins/hermes-os-mission-control/`.

Main endpoints: `GET /health`, `GET /runtime`, `GET /services`, `GET /snapshot`, `GET /conversation`, `POST /talk`, `POST /voice`, `POST /tts`, `GET /approvals`, `POST /approvals/{approval_id}/approve`, `POST /approvals/{approval_id}/deny`.

## Development layout

```text
plugin.yaml                 # Hermes plugin manifest
__init__.py                 # no-op plugin registration hook
dashboard/manifest.json     # Dashboard tab/API manifest
dashboard/plugin_api.py     # FastAPI router
dashboard/dist/             # Generated dashboard assets loaded by Hermes
src/frontend/index.html     # Source single-file Mission Control UI
src/plugin/                 # Source copy of plugin backend/manifest
```

## Safety notes

Mission Control is intended as a local cockpit for your own Hermes installation. Do not expose your Hermes dashboard publicly without authentication. File/config/install/delete/external-send actions should remain protected by Hermes approvals.

## License

MIT
