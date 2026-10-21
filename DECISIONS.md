# Decisions

What was adapted from prior art, what was written fresh, and why. Prior art was
read on 2026-10-20 (shallow clones of each repo's default branch).

## License check

The spec says both main references are MIT. Only one of them could be verified:

| Repo | LICENSE file | GitHub license metadata | Notes |
|---|---|---|---|
| CodeForBreakfast/claude-usage-exporter | **none** | none | No license grant, so default copyright applies. **No code copied.** |
| jimdawdy-hub/claude-usage-tracker | **none** | none | README says "MIT", but there's no license text or copyright line. Its `scanner.py` says it is adapted from phuryn/claude-usage (MIT). **No code copied.** |
| phuryn/claude-usage | MIT, © 2026 Pawel Huryn | MIT | Origin of the JSONL scanning approach. Adapted (see below). |
| georgewhewell/quota-exporter | MIT, © 2026 George Whewell | MIT | OAuth endpoint, headers, and the read-only rule. Written fresh from its documented behavior. |

Algorithms and API facts (endpoint paths, field names, the polling state
machine) aren't copyrightable expression, so they were reimplemented
from their description, with attribution below. The only adapted code is
the phuryn-derived JSONL logic in `cusage/cctokens.py`. Its notice is in
`THIRD_PARTY_NOTICES.md`.

## Architecture: SQLite + Notion, not Prometheus + Grafana

**Agree with the spec's rejection; built as specified.** Prometheus would need
`--storage.tsdb.retention.time` raised from its 15-day default to keep the
history this project exists to keep. It also adds two daemons to run on a Pi
for one row per minute at most. The thing Prometheus is genuinely better at,
charting the intra-day curve, is partly covered here: `polls` keeps the full
change-point series, so a Grafana SQLite datasource or a notebook can be
pointed at `usage.db` later without changing anything. Notion stays the
week-over-week view.

## HTTP client and Cloudflare

**Idea from jimdawdy (`scraper.py`), code written fresh.** It uses `curl_cffi`
with Firefox TLS impersonation (`firefox133`) for claude.ai.

- **curl_cffi from the start, not "requests first, fall back on challenge".** A
  fallback adds a second dependency and a code path that runs only while
  things are already going wrong. curl_cffi has aarch64 wheels and a
  requests-like API, so it's the one third-party dependency. Notion and Slack
  use stdlib `urllib`.
- **The User-Agent comes from the impersonation profile** rather than a
  hand-set desktop UA string. A UA that disagrees with the TLS fingerprint is
  exactly what bot detection looks for. The profile can be changed with
  `CLAUDE_IMPERSONATE`.
- **HTML is never data.** `client.classify()` treats any of these as an auth-class
  failure: `cf-mitigated: challenge`, a "Just a moment" body, an HTML
  content-type, or a body starting with `<`. Redirects are not followed, so a
  login redirect shows up as a non-200 instead of an HTML 200. Raw bodies of
  failures are not stored.
- Timeout 20 s; one retry after 60 s on network error or 5xx; no retry on
  401/403. During the daemon, the 60 s wait is interruptible by SIGTERM.
- Verified live on 2026-10-20 with a garbage key: `/api/organizations` returned
  a JSON 403 (no Cloudflare challenge), recorded as `ok=0, http_status=403`.

## Response shape

**Spec correction:** the field is `utilization`, not `utilization_pct`. All
three code-bearing references agree on `utilization` and `resets_at`:
claude-usage-exporter `claude.go`, jimdawdy `scraper.py`, and quota-exporter
`anthropic.py`. The parser also accepts `utilization_pct` / `percent` as
fallbacks.

- `five_hour`, `seven_day`: expected. A missing key logs a warning, stores
  NULL, and fires the one-time schema alert. A key that is present but `null`
  (no active window) is **not** treated as a schema change.
- Any other top-level object with a utilization field, such as
  `seven_day_opus`, `seven_day_sonnet` or `seven_day_oauth_apps`, goes into
  `poll_buckets` automatically.
- `extra_usage.used_credits` and `extra_usage.monthly_limit` are in **cents**
  (jimdawdy). `credits_spent_month = used/100`. `credits_balance =
  (monthly_limit - used)/100`, which is headroom under the monthly
  extra-usage cap, not a prepaid wallet. The OAuth endpoint may instead report
  `spend.used.amount_minor`/`exponent` (quota-exporter), which is used as a
  fallback.
- **Not yet verified against a logged-in response.** I had no session key while
  building, so the browser network-tab check the spec asks for still needs
  doing. `claude-usage poll --print-raw` prints the live response for that
  comparison. If it differs, fix `cusage/parse.py`, then run `backfill-parse`.

**Plan detection** (jimdawdy: `monthly_limit` 2000/10000/20000 → Pro/Max5/Max20)
is shown in `status` as "Plan (heuristic)" but never stored. `monthly_limit` looks
like the user-adjustable extra-usage spending cap, so treating it as a plan
signal is fragile.

## Auth

The abstraction is `client.Auth` with a `fetch_usage(sleep)` method. Two
implementations exist, selected by `CLAUDE_AUTH_MODE`.

- **CookieAuth (default):** `Cookie: sessionKey=…`. The org comes from
  `CLAUDE_ORG_ID`, or is discovered from `/api/organizations` by preferring
  the org whose `capabilities` include `chat` and cached for the life of the
  process.
- **OAuthAuth (implemented, not default):** endpoint and the
  `anthropic-beta: oauth-2025-04-20` header follow quota-exporter. The file is
  opened read-only and re-read every poll. An expired `expiresAt` is reported
  as an auth failure telling you to run `claude`, never as a refresh. Nothing
  in the code can write that file. Before switching to it, compare its numbers
  with Settings > Usage, as the spec asks. That's the remaining follow-up.

## Adaptive polling

**Algorithm from claude-usage-exporter `poller.go`; reimplemented in
`cusage/poller.py::AdaptiveInterval` (no code copied, since there's no
license).** Same rules: fast 30 s while session or weekly utilization changes,
doubling after 3 unchanged polls up to 5 min, snap to fast when a reset is
under 2 min away. Deliberate differences:

1. **The sleep is capped so a poll lands inside the burst window.** Upstream,
   idling at 5 min, can poll at T-3 min and then at T+2 min, skipping the
   2-minute window entirely. `sleep_for()` wakes at T - window/2.
2. **Failures back off** (double, up to the ceiling) instead of keeping the
   current interval. Hitting claude.ai every 30 s with a dead cookie is how
   you earn a Cloudflare challenge.
3. **The first poll counts as a change.** Upstream initializes the previous
   value to 0.0, so a real 0 % reading looks "unchanged".
4. The daemon **re-reads `.env` when its mtime changes**, so replacing an
   expired session key needs no restart.

Upstream's recommendation score (multi-account routing) is not relevant
here and was not ported.

## Persistence

As specified, a `polls` row is written when a value changes, at least every
15 min (`POLL_HEARTBEAT_SECONDS`), and on every failure. Details:

- "Changes" compares percentages, credits, buckets, and reset times at
  **minute precision**, because `resets_at` carries sub-second jitter that
  would otherwise persist every poll.
- **Additions to the spec schema:** `polls.interval_s` (the interval chosen
  after that poll, for the acceptance check on back-off) and `polls.auth_mode`.
  The **`poll_tally`** table counts every attempt per local day. `daily.poll_count`
  comes from it, because persisted rows alone would undercount successful polls
  by design. `daily.source_fingerprint`, `needs_sync` and `updated_at` drive
  recompute and re-sync.
- Poller state (`state` table) and alert state (`alert_state`) survive restarts.

## Rollup

- A day is a calendar day in `TZ_NAME`, converted to a DST-safe UTC range.
- Yesterday is always recomputed. Any of the 7 days before that is recomputed
  if it has polls and either no `daily` row or a different fingerprint (count,
  max id, and sums of the parsed columns), so `backfill-parse` changes are
  picked up. `rollup --all` recomputes everything.
- `credits_spent_day` sums increases in `credits_spent_month`. A decrease is
  the monthly reset, and the new value counts as spend since the reset. The
  baseline is the last reading before midnight, but only if it is under 24 h
  old. Otherwise an outage would dump several days of spend onto one day.
- Rollup never touches the `cc_*` columns or Notion IDs, and it sets
  `needs_sync` only when a metric actually changed.

## Notion

**Written fresh, stdlib `urllib`.**

- API version `2022-06-28`, which uses the `databases/{id}/query` endpoints.
  A database with multiple data sources (a 2025 Notion feature) would need
  the newer API. A plain single-table database is fine.
- `sync` verifies the 11 properties and their types first, and exits with the
  list of missing or mistyped ones.
- Upsert order: PATCH the stored page ID. If it is gone or archived, query the
  database for `Day == YYYY-MM-DD`; then create. The DB is committed after each
  row, so a crash midway can't cause duplicates next run.
- Requests are at least 0.35 s apart. Retries cover 429 (honoring
  `Retry-After`), 5xx and network errors, with exponential back-off, up to 6
  attempts.
- `NOTION_DATABASE_ID` accepts a bare ID, a dashed UUID, or the full URL. The
  `?v=` view ID is ignored; a test caught that bug.

## Alerting

As specified, with state persisted in `alert_state`:

- An auth failure alerts immediately.
- Other failures alert on the 3rd in a row.
- Either kind reminds at most every 12 h.
- Recovery sends one message, and only if a failure alert was actually sent.
- The schema alert fires once per distinct set of warnings.
- The weekly threshold alerts once per window, keyed by `weekly_resets_at`.
- If Slack itself is down, the alert isn't marked sent, so it retries on the
  next poll.

Additions, so that breakage is never silent:

- Nightly jobs that raise post a Slack message with the error.
- `rollup` alerts if the poller hasn't run for an hour.
- The poller unit has `OnFailure=` pointing at a notifier, for when systemd
  gives up restarting it (5 crashes in 10 min).
- An unexpected exception inside a poll counts as a failed poll for alerting.

## Claude Code tokens (`cc-tokens`)

**Adapted from phuryn/claude-usage `scanner.py` (MIT), via jimdawdy's copy.**

Kept as in upstream:
- Recursive `*.jsonl` scan.
- Skip files whose mtime is unchanged; size is now checked too.
- Within a file, the last streamed record per `message.id` wins.

Changed:
- **Upsert across files.** Upstream uses `INSERT OR IGNORE`, which keeps the
  first (possibly partial) tally when a file is rescanned.
- **A stable synthetic key** (`noid:{sessionId}:{uuid}`) for records without
  a message ID. Upstream re-inserts those on every rescan.
- **Bucketed by local day.** Only `daily.cc_*` columns are written.

Pricing, sessions and tool tables were dropped as out of scope. `ccusage` was
not used because it would add a Node runtime to the Pi. If
`CLAUDE_CODE_LOG_DIR` is unset, the job logs one line and exits 0.

## Security

- `.env` is parsed by the app, not by systemd `EnvironmentFile`. That way manual
  runs see the same config, and secrets aren't in the unit's environment
  (`systemctl show`). Mode 600, owner `claudeusage`. `install.sh` creates it
  from the empty template only if missing.
- Each configured secret is registered with a scrubber. Every log line,
  stored `error` and Slack message passes through it. It also redacts
  `sk-ant-…`, `sessionKey=…`, `Bearer …`, `ntn_…`/`secret_…` and Slack webhook
  URLs. A test checks that a secret embedded in an error never reaches SQLite.
- systemd hardening: `ProtectSystem=strict`, read-only home,
  `ReadWritePaths=/opt/claude-usage`, `NoNewPrivileges`,
  `RestrictAddressFamilies`, `UMask=0077`.
- Outbound hosts: `claude.ai` (or `api.anthropic.com`), `api.notion.com`,
  `hooks.slack.com`. No telemetry.

## Layout

The spec asks for a single `claude_usage.py` CLI. That file exists as a
10-line entry point over the `cusage/` package (named so it doesn't shadow
`claude_usage.py`), so each component can be tested in isolation. Tests use
stdlib `unittest` (no pytest dependency): `python3 -m unittest discover -s tests -t .`.
