#!/usr/bin/env bash
# Bring this copy of optlab up to date with its branch on GitHub, then restart the app if its server code changed.
# Safe to run any time: it does nothing when already up to date, and it never overwrites your own work.
#
#   bash scripts/autoupdate.sh            run once by hand
#   bash scripts/install_autoupdate.sh    run it every 5 minutes in the background
#
# It only fast-forwards: if this copy has local commits, or edits to files that came from GitHub, it leaves
# everything as it is and says so. Open pages notice the new version within a minute and reload themselves.
set -euo pipefail
cd "$(dirname "$0")/.."

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" = "HEAD" ]; then echo "optlab: not on a branch; not updating"; exit 0; fi
git fetch --quiet origin "$branch"
old="$(git rev-parse HEAD)"
new="$(git rev-parse "origin/$branch")"
[ "$old" = "$new" ] && exit 0

if ! git merge-base --is-ancestor HEAD "origin/$branch"; then
  echo "optlab: this copy has commits that origin/$branch doesn't; not updating"
  exit 0
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "optlab: local edits to tracked files; not updating (git stash them, then run this again)"
  exit 0
fi

git merge --ff-only --quiet "origin/$branch"
echo "optlab: updated ${old:0:7} -> ${new:0:7}"
changed="$(git diff --name-only "$old" "$new")"

if grep -qx 'requirements.txt' <<<"$changed" && [ -x .venv/bin/pip ]; then
  echo "optlab: installing new requirements"
  .venv/bin/pip install --quiet -r requirements.txt
fi
# The page's own files are served straight from disk: only server code needs a restart.
if grep -qE '\.py$|^requirements\.txt$' <<<"$changed"; then
  if systemctl --user restart optlab-api 2>/dev/null; then echo "optlab: restarted optlab-api"
  else echo "optlab: restart optlab-api yourself to load the new server code"; fi
fi
