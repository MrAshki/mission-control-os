# Setup Guide

## Quick setup

1. Install Hermes Agent.
2. Install this plugin:

```powershell
hermes plugins install MrAshki/mission-control-os --enable
```

3. Restart/start the dashboard:

```powershell
hermes dashboard --no-open
```

4. Open `http://127.0.0.1:9119/mission-control`.

## Updating

```powershell
hermes plugins update hermes-os-mission-control
```

Restart the dashboard after updates.

## Uninstalling

```powershell
hermes plugins disable hermes-os-mission-control
hermes plugins remove hermes-os-mission-control
```

## Optional voice setup

Voice input/output is optional. If you want it, install/configure local Whisper.cpp, Piper, and ffmpeg, then point the plugin at their directories if needed:

```powershell
$env:HERMES_MISSION_CONTROL_WHISPER_DIR = "$HOME\.config\whisper"
$env:HERMES_MISSION_CONTROL_PIPER_DIR = "$HOME\.config\piper"
```
