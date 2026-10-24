#!/usr/bin/env bash
# Installs Calibre (provides ebook-convert) for Book Buddy's Kindle-format support.
set -euo pipefail
sudo pacman -S --needed calibre
ebook-convert --version | head -1
