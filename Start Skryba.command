#!/usr/bin/env bash
# Double-click launcher: pulls the latest, starts Skryba if it isn't already
# running, and opens it in your browser. Resolves its own folder so this
# works wherever the repo is cloned.
cd "$(dirname "${BASH_SOURCE[0]}")"
./run.sh
echo
echo "You can close this window — Skryba keeps running in the background."
sleep 3
