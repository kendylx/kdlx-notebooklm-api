#!/usr/bin/env bash
# Push a host-side notebooklm login session into the running bridge container.
#
# Usage:
#   ./push-profile.sh <profile> [storage_state_path]
#
# Examples:
#   notebooklm -p p2 login                # sign in account #2 (isolated profile)
#   ./push-profile.sh p2                   # upload profiles/p2/storage_state.json
#   ./push-profile.sh p3 ~/.notebooklm/profiles/p3/storage_state.json
#
# Env:
#   NLM_BRIDGE_TOKEN  bearer token (default: read from compose.yml fallback)
#   NLM_BRIDGE_URL    default http://127.0.0.1:8767
set -euo pipefail

PROFILE="${1:?usage: push-profile.sh <profile> [storage_state_path]}"
STATE="${2:-$HOME/.notebooklm/profiles/$PROFILE/storage_state.json}"
URL="${NLM_BRIDGE_URL:-http://127.0.0.1:8767}"

if [ ! -f "$STATE" ]; then
  echo "not found: $STATE" >&2
  exit 1
fi

TOKEN="${NLM_BRIDGE_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  TOKEN="$(cd "$(dirname "$0")" && docker compose exec -T nlm-bridge printenv NLM_BRIDGE_TOKEN 2>/dev/null | tr -d '[:space:]')"
fi
if [ -z "$TOKEN" ]; then
  echo "NLM_BRIDGE_TOKEN not set and could not read it from the container" >&2
  exit 1
fi

python3 - "$PROFILE" "$STATE" "$URL" "$TOKEN" <<'PY'
import json, sys, urllib.request
profile, path, url, token = sys.argv[1:5]
state = json.load(open(path))
body = json.dumps({
    "profile": profile,
    "cookies": state["cookies"],
    "origins": state.get("origins", []),
}).encode()
req = urllib.request.Request(
    url + "/auth", data=body,
    headers={"Authorization": "Bearer " + token,
             "Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req, timeout=60).read().decode())
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode())
    sys.exit(1)
PY
