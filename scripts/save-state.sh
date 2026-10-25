#!/usr/bin/env bash
set -euo pipefail
cd runtime
# Generated site and logs are never committed to the processing state branch.
git add state.db audio
for file in daily.txt deployed-feed.sha256 source_health.json; do
  if [ -f "$file" ]; then git add "$file"; fi
done
if git diff --cached --quiet; then exit 0; fi
git config user.name 'Blogcast worker'
git config user.email 'blogcast@users.noreply.github.com'
# Keep one snapshot so deleted MP3s do not accumulate in branch history.
tree=$(git write-tree)
commit=$(git commit-tree "$tree" -m 'Save Blogcast processing state')
git reset --soft "$commit"
git push --force-with-lease origin HEAD:state
