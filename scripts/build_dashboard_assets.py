from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "frontend" / "index.html"
MANIFEST = ROOT / "dashboard" / "manifest.json"
DIST = ROOT / "dashboard" / "dist"

TEMPLATE = """/* Generated from src/frontend/index.html. */
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.HERMES_PLUGINS || window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS || !PLUGINS.register) return;
  const React = SDK.React;
  const hooks = SDK.hooks || React;
  const e = React.createElement;
  const APPROVED_HTML = __BODY__;
  const APPROVED_SCRIPT = __SCRIPT__;
  function MissionControlOS() {
    const hostRef = hooks.useRef(null);
    hooks.useEffect(function () {
      const host = hostRef.current;
      if (!host) return;
      const previousFetch = window.__HERMES_MISSION_CONTROL_FETCH__;
      window.__HERMES_MISSION_CONTROL_FETCH__ = function (url, options) {
        if (SDK.fetchJSON) return SDK.fetchJSON(url, options || {});
        return fetch(url, options || {}).then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        });
      };
      host.innerHTML = APPROVED_HTML;
      const scriptEl = document.createElement('script');
      scriptEl.type = 'text/javascript';
      scriptEl.text = APPROVED_SCRIPT;
      host.appendChild(scriptEl);
      return function () {
        try { host.innerHTML = ''; } catch (_) {}
        if (previousFetch) window.__HERMES_MISSION_CONTROL_FETCH__ = previousFetch;
        else try { delete window.__HERMES_MISSION_CONTROL_FETCH__; } catch (_) { window.__HERMES_MISSION_CONTROL_FETCH__ = undefined; }
      };
    }, []);
    return e('div', { className: 'mc-open-design-host', ref: hostRef });
  }
  PLUGINS.register(__PLUGIN_NAME__, MissionControlOS);
})();
"""

def main() -> int:
    html = HTML.read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", html, re.S | re.I)
    script = re.search(r"<script>(.*?)</script>\s*</body>", html, re.S | re.I)
    body = re.search(r"<body>(.*?)<script>", html, re.S | re.I)
    if not (style and script and body):
        raise SystemExit("Could not extract style/body/script from src/frontend/index.html")
    plugin_name = json.loads(MANIFEST.read_text(encoding="utf-8"))["name"]
    DIST.mkdir(parents=True, exist_ok=True)
    (DIST / "style.css").write_text(style.group(1).strip() + "\n", encoding="utf-8", newline="\n")
    index_js = TEMPLATE.replace("__BODY__", json.dumps(body.group(1).strip(), ensure_ascii=False)).replace("__SCRIPT__", json.dumps(script.group(1).strip(), ensure_ascii=False)).replace("__PLUGIN_NAME__", json.dumps(plugin_name))
    (DIST / "index.js").write_text(index_js, encoding="utf-8", newline="\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
