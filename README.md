# aismith-status — status.aismith.io

Off-stack fleet status page (GitHub Pages) — **fate-isolated from the hub by rule**:
this page must keep working when Supabase/Netlify/the hub are down.

- `status.json` — written every 15 min by the `update-status` Action: hub
  reachability probe + the hub's public `status-feed` aggregates (signal ages /
  counts only). Feed unreachable → the page shows signals-stale, which is itself
  the signal.
- `incidents.json` — authored from the hub (Fleet → Status → publish-incident,
  contents-API commit) or, **when the hub is down, edited directly here**
  (runbook path). Newest first; `status: open|resolved`.
- Client hubs may PULL `status.json` as a banner feed. Never a push into any
  client DB.
- `updates.json` / `updates.html` -- the public changelog. The same Action's
  `Changelog` step (`scripts/changelog.py`) adds merged pull requests of the
  repositories in `CHANGELOG_REPOS` that carry the label `public-changelog`
  and not `no-changelog`, merged on or after `CHANGELOG_SINCE`: merge time,
  repository name, number and title only, newest first, no links. DORMANT
  until the `CHANGELOG_READ_TOKEN` secret is set and `CHANGELOG_SINCE` holds a
  date; fails closed on any error. Tests: `python3 -m unittest discover -s tests -v`.

Backup signal reads null until backup events flow into the hub feed
(`backup_ok` activity events — future monitor half).
