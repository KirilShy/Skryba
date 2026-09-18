#!/usr/bin/env bash
# Double-click launcher: pulls the latest, starts Skryba if it isn't already
# running, and opens it in your browser. Resolves its own folder so this
# works wherever the repo is cloned.
cd "$(dirname "${BASH_SOURCE[0]}")"
if ./run.sh; then
  echo
  echo "You can close this window — Skryba keeps running in the background."
  sleep 3
else
  echo
  echo "Something went wrong starting Skryba (see above)."
  read -r -p "Press Enter to close this window..." _
fi
