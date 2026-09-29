#!/usr/bin/env bash
# usage.sh — who has used the site: one row per signed-in user, from Application Insights.
#
#   ./scripts/usage.sh            # last 7 days
#   ./scripts/usage.sh 30         # last 30 days
#   ./scripts/usage.sh 7 --events # and the last 50 events, newest first
#
# Events come from the control API (api/src/telemetry.ts): visit (a signed-in page opened),
# session_created, file_uploaded, pairing_checked, pairing_accepted, run_started. Each
# carries the Static Web Apps sign-in name; uploads that arrive on the session's upload
# ticket are attributed through the session that user created. Events take a few minutes
# to appear.
set -euo pipefail

days="${1:-7}"; [[ "$days" =~ ^[0-9]+$ ]] || { echo "days must be a number" >&2; exit 2; }
events="${2:-}"
rg="${AZURE_WEB_RESOURCE_GROUP:-$(gh variable get AZURE_WEB_RESOURCE_GROUP --env production 2>/dev/null || echo rg-strong-loop-web-production)}"
app="$(az monitor app-insights component show -g "$rg" --query "[0].appId" -o tsv 2>/dev/null)"
[[ -n "$app" ]] || { echo "no Application Insights component in $rg (az login?)" >&2; exit 1; }

query() {
  az monitor app-insights query --app "$app" --analytics-query "$1" -o json \
    | python3 -c '
import json, sys
t = json.load(sys.stdin)["tables"][0]
cols = [c["name"] for c in t["columns"]]
rows = [[("" if v is None else str(v)[:19].replace("T", " ") if c.endswith(("seen", "timestamp")) else str(v)) for c, v in zip(cols, r)] for r in t["rows"]]
if not rows:
    print("(no events)"); sys.exit()
w = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
print("  ".join(c.ljust(w[i]) for i, c in enumerate(cols)))
for r in rows:
    print("  ".join(v.ljust(w[i]) for i, v in enumerate(r)))'
}

base="let ev = customEvents | where timestamp > ago(${days}d)
  | where tostring(customDimensions.provider) != 'local-test'
  | extend u = tostring(customDimensions.user), sid = tostring(customDimensions.sessionId);
let owners = ev | where name == 'session_created' and u != '' | summarize owner = take_any(u) by sid;
ev | join kind=leftouter owners on sid
   | extend who = iff(u != '', u, iff(isnotempty(owner), owner, '(unknown)'))"

echo "Usage, last ${days} day(s) — $(date -u '+%Y-%m-%d %H:%M UTC')"
query "$base
| summarize first_seen = min(timestamp), last_seen = max(timestamp),
            visits = countif(name == 'visit'), checks = countif(name == 'pairing_checked'),
            runs = countif(name == 'run_started'),
            own_files = countif(name == 'file_uploaded' and tostring(customDimensions.path) in ('intake/data.csv', 'intake/charter.yaml', 'intake/charter.md'))
  by user = who
| order by last_seen desc"

if [[ "$events" == "--events" ]]; then
  echo; echo "Last 50 events"
  query "$base
| project timestamp, user = who, event = name,
          detail = strcat(tostring(customDimensions.page), tostring(customDimensions.path), ' ',
                          tostring(customDimensions.charterPreset), ' ', tostring(customDimensions.dataPreset))
| order by timestamp desc | take 50"
fi
