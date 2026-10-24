#!/usr/bin/env bash
# Install and start the Book Buddy folder watcher as a systemd USER service (no sudo needed).
# Usage: systemd/install.sh [/path/to/bookbuddy]   (default: the `bookbuddy` found on PATH)
# Folders, voice and notifiers come from ~/.config/bookbuddy/config.toml.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bb="${1:-$(command -v bookbuddy || true)}"
[ -n "$bb" ] && [ -x "$bb" ] || { echo "bookbuddy executable not found; pass its path as the first argument" >&2; exit 1; }
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
sed "s#@BOOKBUDDY@#$bb#" "$here/bookbuddy-watcher.service" > "$unit_dir/bookbuddy-watcher.service"
systemctl --user daemon-reload
systemctl --user enable --now bookbuddy-watcher.service
systemctl --user --no-pager status bookbuddy-watcher.service | head -12
echo "Logs: journalctl --user -u bookbuddy-watcher -f"
