"""Regenerate ngrok/ngrok.yml from ngrok/traffic-policy.yml.

Why this exists: `ngrok start` gives no way to point at a policy FILE.

  · `--traffic-policy-file` is a flag on `ngrok http`, not `ngrok start`.
  · A `traffic_policy: {file: ...}` key under `endpoints:` parses locally but is
    rejected by the server with ERR_NGROK_9026 — it reads the filename itself as
    the policy body.

So the policy has to be inline in ngrok.yml. Keeping only an inline copy would
mean losing every comment explaining WHY each rule exists, and those comments
are the reason the rules are still correct six months from now. Instead the
commented file stays the source of truth and this script copies it in.

    python ngrok/build_config.py

Run it after every edit to traffic-policy.yml, then restart the tunnel.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
POLICY = HERE / "traffic-policy.yml"
CONFIG = HERE / "ngrok.yml"

HEADER = r"""# Tunnel definition for the local trigger API. NO SECRETS LIVE HERE.
#
# The authtoken deliberately stays in ngrok's own global config
# (the ngrok.yml under %LOCALAPPDATA%, written by `ngrok config add-authtoken`),
# so this file is safe to track in git while the credential is not. ngrok merges
# multiple --config files; start_tunnel.ps1 passes both.
#
# Schema v3 - needs agent 3.5+. `ngrok version` must NOT report 3.3.x (the build
# winget ships): it rejects this with "unknown version '3'" and has no
# traffic_policy support at all. start_tunnel.ps1 prefers the C:\ngrok install
# for exactly this reason.
#
# THE TRAFFIC POLICY BELOW IS GENERATED - do not hand-edit it here.
# Edit ngrok/traffic-policy.yml (the readable copy, with comments) and run
#   python ngrok/build_config.py
#
# Why inline: `ngrok start` has no --traffic-policy-file flag (that one is on
# `ngrok http`), and a `traffic_policy: {file: ...}` key under `endpoints:` is
# rejected by the server as ERR_NGROK_9026 - it reads the filename as the
# policy body.
version: "3"

endpoints:
  - name: vfs-webhook
    # Free tier assigns a random *.ngrok-free.dev name that CHANGES on every
    # restart. With a reserved domain, uncomment:
    # url: https://your-reserved-name.ngrok-free.app
    upstream:
      # Loopback upstream: the API binds 127.0.0.1 only, and the tunnel is the
      # single public edge.
      url: http://127.0.0.1:8000
    traffic_policy:
"""


def main() -> int:
    if not POLICY.is_file():
        print(f"Missing {POLICY}", file=sys.stderr)
        return 1

    # Comments are stripped, not carried over: they live in traffic-policy.yml,
    # and duplicating them into a generated file invites the two to disagree.
    body = [
        line for line in POLICY.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not body:
        print("Policy file has no rules — refusing to write an empty policy.",
              file=sys.stderr)
        return 1

    indented = "\n".join("      " + line for line in body)
    CONFIG.write_text(HEADER + indented + "\n", encoding="utf-8")

    print(f"Wrote {CONFIG} ({len(body)} policy lines).")
    print("Validate with:  ngrok config check --config " + str(CONFIG))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
