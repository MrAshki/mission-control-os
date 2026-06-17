"""Mission Control OS plugin package.

The dashboard extension is discovered from dashboard/manifest.json. This register hook is intentionally no-op; the FastAPI router lives in dashboard/plugin_api.py and is loaded by the Hermes dashboard server.
"""

def register(ctx):
    return None
