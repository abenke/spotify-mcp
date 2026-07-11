#!/usr/bin/env bash
# Adds the spotify local MCP server to Claude Desktop's config, merging with any
# existing servers. Safe to re-run.
set -euo pipefail

python3 - <<'PY'
import json, os

path = os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        cfg = json.load(f)

cfg.setdefault("mcpServers", {})["spotify"] = {
    "command": "/Users/abenke/dev/spotify-mcp/.venv/bin/spotify-mcp",
    "env": {
        "SPOTIFY_CLIENT_ID": "3ff7f21fe22e43458049fb27a221d269"
    }
}

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print("Updated:", path)
PY
