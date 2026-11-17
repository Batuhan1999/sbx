"""The page a preview shows while its app can't: one small template for
every state, the same on every runtime.

The gate (gate.py) decides the state; nginx sends a refused request and an
upstream's 502 or 504 here (tls.sh, ip_previews.nginx). A browser's
navigation gets the page; anything else (an XHR, a websocket, an API
client) gets the status with Retry-After where waiting helps. A page that
waits polls the preview host's /.sbx/status (gate.status), backs off to
every few seconds with no time cap, says "Ready" when the app answers and
then replaces itself with the exact URL that was asked for."""
from __future__ import annotations

import dataclasses
import html
import json

# State -> the HTTP status a non-HTML request gets.
STATUS = {"starting": 503, "waking": 503, "no_listener": 503, "app_stopped": 502, "failed": 502,
          "deleted": 404, "missing": 404, "forbidden": 403, "ready": 200}
WAITING = ("starting", "waking", "no_listener")
RETRY_AFTER = "2"
STATUS_PATH = "/.sbx/status"   # on every preview host: nginx sends it to gate.status


@dataclasses.dataclass(frozen=True)
class Page:
    """What a preview shows instead of its app. name is "" for a visitor who
    may not learn it (a neutral page); action is a link for the owner."""
    state: str
    name: str = ""
    step: str = ""
    port: int | None = None
    detail: str = ""
    action: tuple = ()            # (label, url)
    reload: bool = False          # nothing wakes it: the page loads itself again (a navigation wakes it)

    @property
    def waiting(self) -> bool:
        return self.state in WAITING

    @property
    def status(self) -> int:
        return STATUS[self.state]

    def neutral(self) -> "Page":
        """The same state without names: for a visitor who may not learn them."""
        return Page(self.state if self.waiting else "starting", step="Starting")

    def title(self) -> str:
        who = self.name
        if self.state in ("starting", "waking"):
            return f"Starting {who}…" if who else "Starting…"
        return {
            "ready": "Ready",
            "no_listener": f"Nothing is listening on port {self.port} in {who}" if self.port else
                           f"Nothing is listening here in {who}",
            "app_stopped": f"{who}'s app stopped",
            "failed": f"{who} failed",
            "deleted": f"{who} was deleted",
            "missing": f"No sandbox named {who}" if who else "Nothing here",
            "forbidden": "You don't have access",
        }[self.state]

    def line(self) -> str:
        if self.state in ("starting", "waking"):
            return self.step or ("Waking up" if self.state == "waking" else "Starting")
        return {
            "ready": "Opening the app",
            "no_listener": "Start a server on it. This page opens it when it answers.",
            "app_stopped": self.detail or "Restart it to open this page again.",
            "failed": self.detail or "Restart or delete it in sbx.",
            "deleted": "Create it again to open this page.",
            "missing": "Check the link, or list your sandboxes with sbx ls.",
            "forbidden": "Only its owner can open this preview.",
        }[self.state]

    def view(self) -> dict:
        """As the status endpoint answers it."""
        return {"state": self.state, "title": self.title(), "line": self.line(), "waiting": self.waiting,
                "reload": self.reload,
                **({"action": {"label": self.action[0], "url": self.action[1]}} if self.action else {})}


def wants_html(method: str, accept: str, upgrade: str = "") -> bool:
    """A browser's navigation: a GET (or HEAD) asking for HTML, not a websocket."""
    return method in ("GET", "HEAD") and "text/html" in (accept or "") and not upgrade


def headers(page: Page) -> dict:
    found = {"Cache-Control": "no-store", "X-Sbx-Page": page.state}
    if page.waiting:
        found["Retry-After"] = RETRY_AFTER
    return found


def plain(page: Page) -> str:
    """The body a non-HTML request gets."""
    return json.dumps({"error": page.state, "title": page.title(), "detail": page.line()})


def render(page: Page, *, reason: str = "denied", login: str = "") -> str:
    """The page, light or dark as the browser prefers. reason: why nginx
    sent the request here ("unavailable": nginx could not reach the app, so a
    handover that meets it again is counted and stopped); login: where a poll
    answered 401 sends the browser (with next= this page's URL)."""
    action = (f'<a class="action" href="{html.escape(page.action[1])}">{html.escape(page.action[0])}</a>'
              if page.action else "")
    data = json.dumps({"waiting": page.waiting, "status": STATUS_PATH, "reason": reason,
                       "port": page.port, "login": login}).replace("<", "\\u003c")
    return TEMPLATE.format(title=html.escape(page.title()), line=html.escape(page.line()), action=action,
                           state=html.escape(page.state), data=data,
                           busy="true" if page.waiting else "false")


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; --bg: #fafafa; --ink: #18181b; --muted: #63636e; --line: #e7e7ea;
  --accent: #4f46e5; --accent-ink: #ffffff; --ok: #15803d; --ease-out: cubic-bezier(0.23, 1, 0.32, 1); }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #0c0c0e; --ink: #ececf0; --muted: #a0a0ab; --line: #26262c; --accent: #8b8cf8;
    --accent-ink: #0c0c0e; --ok: #4ade80; }}
}}
* {{ box-sizing: border-box; }}
html, body {{ height: 100%; margin: 0; }}
body {{ background: var(--bg); color: var(--ink); display: grid; place-items: center; padding: 16px;
  font: 14px/1.5 "Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased; }}
main {{ width: min(360px, 100%); transition: opacity 200ms var(--ease-out), transform 200ms var(--ease-out); }}
@starting-style {{ main {{ opacity: 0; transform: translateY(4px); }} }}
main.leaving {{ opacity: 0; transform: translateY(-4px); }}
h1 {{ font-size: 15px; font-weight: 550; margin: 0 0 4px; letter-spacing: -0.005em; overflow-wrap: anywhere; }}
p {{ margin: 0; color: var(--muted); min-height: 1.5em; transition: opacity 160ms ease; }}
.bar {{ height: 2px; margin-top: 16px; background: var(--line); border-radius: 2px; overflow: hidden; }}
.bar::after {{ content: ""; display: block; width: 32%; height: 100%; background: var(--accent); border-radius: 2px;
  animation: slide 1.4s linear infinite; }}
[aria-busy="false"] .bar {{ display: none; }}
main.ready .bar::after {{ animation: none; width: 100%; background: var(--ok); transition: width 200ms var(--ease-out); }}
@keyframes slide {{ from {{ transform: translateX(-100%); }} to {{ transform: translateX(320%); }} }}
.action {{ display: inline-block; margin-top: 14px; padding: 6px 12px; border-radius: 6px; background: var(--accent);
  color: var(--accent-ink); text-decoration: none; font-weight: 500; transition: transform 160ms var(--ease-out); }}
.action:active {{ transform: scale(0.97); }}
@media (prefers-reduced-motion: reduce) {{
  main, main.leaving {{ transform: none; }}
  @starting-style {{ main {{ transform: none; }} }}
  .bar::after {{ animation: none; width: 100%; opacity: 0.5; }}
}}
</style>
</head>
<body>
<main aria-busy="{busy}" aria-live="polite" data-state="{state}">
<h1 id="title">{title}</h1>
<p id="line">{line}</p>
<div class="bar" role="progressbar" aria-label="{title}"></div>
<span id="act">{action}</span>
</main>
<script>
(function () {{
  var d = {data}, main = document.querySelector("main");
  if (!d.waiting) return;
  var wait = 1000, key = "sbx-handover:" + location.href, reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  function show(v) {{
    document.getElementById("title").textContent = v.title;
    document.getElementById("line").textContent = v.line;
    document.title = v.title;
    main.dataset.state = v.state;
    main.setAttribute("aria-busy", v.waiting ? "true" : "false");
    var act = document.getElementById("act");
    act.textContent = "";
    if (v.action) {{ var a = document.createElement("a"); a.className = "action"; a.href = v.action.url;
      a.textContent = v.action.label; act.appendChild(a); }}
  }}
  function handover() {{
    var tries = [];
    try {{ tries = JSON.parse(sessionStorage.getItem(key) || "[]").filter(function (t) {{ return Date.now() - t < 60000; }}); }} catch (e) {{}}
    if (d.reason === "unavailable" && tries.length >= 2) {{
      show({{state: "failed", title: "The app answered with an error", waiting: false,
        line: "It keeps failing on port " + (d.port || "") + ". Check its logs (sbx logs), then reload."}});
      return;
    }}
    try {{ tries.push(Date.now()); sessionStorage.setItem(key, JSON.stringify(tries)); }} catch (e) {{}}
    show({{state: "ready", title: "Ready", line: "Opening the app", waiting: false}});
    main.classList.add("ready");
    setTimeout(function () {{
      main.classList.add("leaving");
      setTimeout(function () {{ location.replace(location.href); }}, reduce ? 0 : 200);
    }}, reduce ? 150 : 350);
  }}
  function again() {{ setTimeout(poll, wait = Math.min(wait * 1.5, 5000)); }}
  function poll() {{
    fetch(d.status, {{credentials: "same-origin", cache: "no-store", headers: {{"Accept": "application/json"}}}})
      .then(function (r) {{
        if (r.status === 401 && d.login) {{ location.replace(d.login + "?next=" + encodeURIComponent(location.href)); return; }}
        if (!r.ok) return again();
        return r.json().then(function (v) {{
          if (v.state === "ready") return handover();
          show(v);
          if (v.reload) return setTimeout(function () {{ location.replace(location.href); }}, wait);
          if (v.waiting) again();
        }});
      }}).catch(again);
  }}
  setTimeout(poll, wait);
}})();
</script>
</body>
</html>
"""
