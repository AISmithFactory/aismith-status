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

Backup signal reads null until backup events flow into the hub feed
(`backup_ok` activity events — future monitor half).
