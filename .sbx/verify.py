#!/usr/bin/env python3
"""Use the dev installation the way an operator does, without printing secrets:
its admin commands make an admin and an API token, the API answers as that
admin, Boat answers with the repository's key, and a marker person is added
(--seed) or reset (--reset). Run from the checkout with the app's env:
  . /run/sbx/app-env; .venv/bin/python .sbx/verify.py [--seed|--reset|--expect-marker|--expect-no-marker]"""
import argparse, json, os, subprocess, sys, urllib.error, urllib.request

p = argparse.ArgumentParser()
g = p.add_mutually_exclusive_group()
for flag in ("--seed", "--reset", "--expect-marker", "--expect-no-marker"):
    g.add_argument(flag, action="store_true")
a = p.parse_args()
BASE = "http://127.0.0.1:8080"
ADMIN = os.environ["SBX_DEV_ADMIN_EMAIL"]
MARKER = "sbx-check-marker@example.com"


def admin(*args, check=True):
    r = subprocess.run([sys.executable, "-m", "sbx_control.admin", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"sbx-admin {args[0]} {args[1] if len(args) > 1 else ''}: exit {r.returncode}: {r.stderr.strip()[-300:]}")
    return r


def call(path, token, method="GET"):
    req = urllib.request.Request(BASE + path, method=method, headers={"Authorization": f"Bearer {token}"},
                                 data=b"{}" if method == "POST" else None)
    if method == "POST":
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def people():
    return admin("user", "ls").stdout


if ADMIN not in people():
    admin("user", "add", ADMIN, "--admin")
token = admin("token", "create", ADMIN, "--label", "verify").stdout.strip()
out = {}
status, me = call("/api/me", token)
assert status == 200 and isinstance(me, dict) and me.get("email") == ADMIN, f"/api/me: {status} {me}"
out["api_me_as_admin"] = True
status, rt = call("/api/runtimes", token)
assert status == 200, f"/api/runtimes: {status} {rt}"
kinds = [r.get("kind") for r in rt.get("runtimes", [])] if isinstance(rt, dict) else []
out["runtimes"] = kinds
if "boat" in kinds:
    status, checks = call("/api/runtimes/boat/test", token, "POST")
    found = checks.get("checks", checks) if isinstance(checks, dict) else checks
    boat = [c for c in found if isinstance(c, dict) and c.get("name") == "Boat API"]
    assert status == 200 and boat and boat[0].get("ok"), f"Boat test: {status} {checks}"
    out["boat_api"] = boat[0].get("detail")
if a.seed and MARKER not in people():
    admin("user", "add", MARKER)
if a.reset and MARKER in people():
    admin("user", "rm", MARKER)
listed = people()
out["marker"] = MARKER in listed
out["people"] = sum(1 for line in listed.splitlines() if "@" in line)
if a.expect_marker:
    assert out["marker"], "the marker person is gone"
if a.expect_no_marker:
    assert not out["marker"], "the marker person is here"
# The token was for this run only.
for line in admin("token", "ls", ADMIN).stdout.splitlines():
    if " verify " in f" {line} ":
        admin("token", "revoke", line.split()[0], check=False)
print(json.dumps(out))
