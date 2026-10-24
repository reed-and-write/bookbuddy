#!/usr/bin/env bash
# Install (and optionally start) the Book Buddy server as a systemd USER service (no sudo needed).
# Usage: systemd/install-server.sh [/path/to/bookbuddy]   (default: the `bookbuddy` found on PATH)
# Only one of the server and the foreground tools owns the GPU at a time: while the server runs, `bookbuddy watch` and
# `bookbuddy listen` hand their books to it. Settings and API keys live in ~/.config/bookbuddy/config.toml.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bb="${1:-$(command -v bookbuddy || true)}"
[ -n "$bb" ] && [ -x "$bb" ] || { echo "bookbuddy executable not found; pass its path as the first argument" >&2; exit 1; }
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
sed "s#@BOOKBUDDY@#$bb#" "$here/bookbuddy-server.service" > "$unit_dir/bookbuddy-server.service"
systemctl --user daemon-reload
systemctl --user enable --now bookbuddy-server.service
systemctl --user --no-pager status bookbuddy-server.service | head -12
echo "Open http://localhost:8765   Logs: journalctl --user -u bookbuddy-server -f"
