# KGP Crew Monitoring System (Nirnaya-Sārathi) — Project State

_Last reviewed: 29 July 2026_

## Overview
Flask + SQLite application that tracks locomotive crew duty status (sign-on, sign-off, current location, PDD, loco/train numbers) by merging data from three sources: a CMS-portal scraper, manual crew self-reporting via a web form, and admin overrides. A companion RTIS scraper thread tracks live loco GPS positions and can auto-update crew "current location."

## Architecture / Stack
- **Backend**: Flask, single `app.py`, SQLite (WAL mode) via `sqlite3`.
- **Background threads** (daemon, started once via `WERKZEUG_RUN_MAIN` guard):
  - CMS scraper subprocess supervisor (`run_scraper_thread`) — restarts `scraper/login.py` every 30s if it exits, respects `stop.txt` / `fatal_error.txt` sentinel files.
  - RTIS scraper subprocess supervisor (`run_rtis_thread`) — same pattern, separate sentinel files (`rtis_stop.txt`, `rtis_fatal_error.txt`).
  - Hourly cleanup thread — hard-deletes submissions/duty-log rows older than 30 days and soft-deleted crew_records, batched in chunks of 500.
- **DB safety nets**:
  - `recover_db_if_malformed()` — integrity_check on startup, iterdump-based recovery if corrupted, corrupted file backed up with timestamp.
  - `_recover_wal_if_needed()` — checkpoints/removes stray WAL files on startup.
  - Global error handler force-exits the process (`os._exit(1)`) on `disk I/O error` / `database is locked` / `readonly` SQLite errors, relying on Docker/Coolify auto-restart to recover.
- **Auth**: session-based, `super_admin` / `admin` roles, passcode-gated signup, forced password change flow, secret-question password reset.
- **External API surface** (all gated by `X-API-Secret` header, `API_SECRET` env var): `/api/sync` (CMS scraper → crew_records), `/api/sync_ta`, `/api/rtis/*` (loco location sync, active-loco lookup), `/api/ocr_usage`, `/api/scraper/state`.
- **Deployment**: Oracle Cloud Always Free VM via Coolify, public endpoint `kgpcrewmonitor.duckdns.org` (see [[kgp-crew-monitoring]] memory for infra/CAPTCHA/tunnel details — not duplicated here).

## Data Model (SQLite tables)
- `crew_records` — one row per crew, CMS-synced fields, `is_active` flag, soft-delete on sign-off.
- `crew_submissions` — append-only log of manual form submissions; latest row per crew_id wins in merges.
- `crew_duty_log` — append-only 30-day archive of every duty cycle (for non-submitter reporting even after active rows are recycled).
- `admin_edits` — per-field admin override table, keyed `(crew_id, field)`, always highest priority in merges.
- `booking_ta_crew` — phone numbers / TA ordering time.
- `loco_locations` — latest + previous GPS fix per loco, feeds "current location" override.
- `cms_settings`, `users`, `signup_passcodes`, `ocr_usage_log`.

## Key Business Logic Worth Remembering
- **Merge priority** for editable fields (loco/train/bpc/location/CTO/relief): `admin_edits` > `crew_submissions` (latest) > `crew_records` (CMS). Implemented independently in `_get_crew_list_data` and `_build_duty_sheet_data` — **any change to this logic must be mirrored in both places**.
- **Sign-on matching**: a submission is only considered "for the current duty" if its `sign_on_time` is within 60 minutes of the CMS record's sign_on_time (`sign_on_match`). Prevents a stale submission from a previous duty leaking into a new one.
- **Retroactive stale-data cleanup**: on every `/crew_list` load, admin edits / submissions / relief data older than the current `sign_on_time` are deleted in a deferred batch at the end of `_get_crew_list_data` (not inline) to keep the DB write lock window short.
- **PDD (Physical Departure Delay?) calculation**: station-cluster logic (`lobbies` set, NMP-cluster normalization) determines whether a crew is "at a PDD-eligible station"; PDD auto-freezes when a departure_time or RTIS-detected movement is recorded.
- **RTIS location override**: only applied when `is_relief != 1` and loco_no did not come strictly from CMS (`_src['loco_no'] != 'cms'`). Latest RTIS fix (by timestamp) wins over manual location if it's newer.
- **Miss-count sign-off**: a crew missing from 2 consecutive CMS sync payloads (`MISS_THRESHOLD = 2`, ~1hr at 30-min intervals) is treated as auto-signed-off.
- **New-shift detection**: if incoming CMS sign_on_time differs from stored one by >1hr (or string mismatch when unparseable), treated as a new duty — wipes admin_edits/submissions and resets `found_in_ns`.
- **Handover / relief flow**: submitting crew A's relief marks crew B's next duty by inheriting loco/train/bpc and setting crew B's start point to the relief station — `from_sttn` is deliberately **not** overwritten (kept from crew B's own CMS record).
- **RTIS-vs-manual departure tracking**: when RTIS auto-freezes a `departure_time`, an `admin_edits` row `field='is_rtis_departure', value='1'` is written alongside it, so the frontend can tell an RTIS-generated departure apart from a human-entered one and style it accordingly (bold blue text, faint blue background — same treatment as RTIS-generated "Current Location"). `/api/admin_edit` explicitly `DELETE`s this flag whenever an admin manually edits `departure_time`, so a manual override correctly clears the RTIS styling.
- **Duty Hours / PDD bug fixes (resolved)**: previously Duty Hours color badges could go missing, PDD could render completely blank, and both local and server-side sorting could fail. Root causes and fixes:
  - The PDD calculation block is indentation-sensitive — it must sit at exactly 8 spaces inside `_get_crew_list_data`; over-indenting it (e.g. to 12 spaces) silently nests it inside a preceding `if` block and it never runs. This was the root cause of blank PDD.
  - The RTIS override section used to `continue` mid-loop to skip crews that don't need an RTIS check, which also skipped the *rest* of that iteration — including the Duty Hours (`_duty_minutes`) and `_sign_on_iso` calculations for that row. Fixed by introducing a `run_rtis_check` boolean and wrapping the RTIS-only logic in `if run_rtis_check:` instead of using `continue`.
  - Frontend `updateDutyHours` (in `crew_list.html`) depends entirely on a valid ISO datetime string in the `data-signon` attribute (populated from `_sign_on_iso`); if the backend doesn't populate it, that row silently gets no Duty Hours badge with no visible error.
  - `list_sort_key(r)` reads `r.get('_duty_minutes', 0)` rather than `r['_duty_minutes']` specifically to avoid a `KeyError`/500 if some row bypasses the calculation logic.

## Scraper Subprocesses (`scraper/login.py`, `scraper/rtis_scraper.py`)

### Shared patterns
- Both scrapers are long-running loops in their own process (spawned/supervised by `app.py`), controlled via sentinel files in `data/` (`stop.txt`/`rtis_stop.txt`, `run_now.txt`/`rtis_run_now.txt`, `fatal_error.txt`/`rtis_fatal_error.txt`) rather than signals/IPC — simple and survives process restarts.
- `interruptible_sleep()` polls stop/run-now signals every 1s during any wait, so an admin-triggered stop or manual "run now" takes effect within a second instead of waiting out a 25–35 min cycle.
- `human_delay()` / `human_type()` — deliberately randomized delays and char-by-char typing to make Selenium interaction look human (anti-bot-detection measure for both CMS and RTIS portals).
- **Dual-tunnel SOCKS5 failover**: both scrapers test a "Office PC" tunnel on port 1080 first, then a "Laptop" fallback on port 1081 (`get_active_proxy()` / `is_proxy_available()`), each verified with both a raw TCP connect check *and* an actual HTTPS request to the target portal (not just port-open). In production, if both tunnels are down, they deliberately refuse to fall back to a direct connection (`ACTIVE_TUNNEL = "Offline"`) since Oracle Cloud IPs are blocked by the portals; in local/dev they will attempt direct as a convenience.
- Both report live status back to the Flask app via `POST /api/scraper/state` or `POST /api/rtis/state` (`send_state_to_admin`), which is what powers the admin dashboard's live status card, and both support human-in-the-loop CAPTCHA/OTP solving via `waiting_for_captcha` / `waiting_for_otp` states + `wait_for_admin_input()` polling `/api/*/action`.

### `login.py` (CMS scraper)
- Pulls **Sign-On X-Hours** and **Booking-on-TA** reports from `cms.indianrail.gov.in` by replaying the portal's internal XML-payload POST requests (not a public API) after establishing a session either from saved cookies or a fresh Selenium login.
- **CAPTCHA solving cascade**: ddddocr (local, fast) for the first 5 attempts → Google Cloud Vision OCR (attempts 6–10, using `active_client`) → manual admin solve after 10 consecutive failures. Google Vision usage is logged per-account to `ocr_usage_log` via `/api/ocr_usage`.
- **Multi-account GCP credential rotation**: up to 10 base64-encoded service-account JSON blobs can be supplied via `GCP_CREDENTIALS_B64_1..10` env vars; the active account rotates daily (`current_date.toordinal() % len(GCP_CREDENTIALS)`) — spreads Vision API quota usage across multiple free-tier GCP projects. `_decode_gcp_b64()` handles BOM/encoding quirks (UTF-8 BOM, UTF-16 BOM, latin-1 fallback) since these come from pasted env var values.
- OTP flow supports a saved `otp.txt` reused within the same run once the admin has solved it once, avoiding re-prompting on every cycle if the portal allows OTP reuse briefly.
- **Auth-failure circuit breaker**: 2 consecutive "User Id/Password Does Not Match" responses writes `fatal_error.txt` and hard-exits the subprocess (`sys.exit(1)`) — the supervisor loop in `app.py` then stops auto-restarting until an admin clears the fatal-error state, preventing a bad password from hammering CMS with repeated login attempts.
- Cookie-reuse fast path (`cookie_valid`) is the common case once logged in — recently **fixed a dead-code bug**: `sync_ta_reports()` used to only run right after a fresh Selenium login, so as long as cached cookies stayed valid (the normal case), TA data was never synced in practice. Confirmed via prod logs showing `/api/sync` firing but `/api/sync_ta` never firing. Now TA sync runs every cycle a session is valid, not just post-login.
- Low-memory Chrome flags (`--js-flags=--max-old-space-size=256`, disabled extensions/sync/background-networking) are tuned for the 1 GB RAM Oracle free-tier VM — don't remove without checking OOM behavior on that box.

### `rtis_scraper.py` (RTIS loco-location scraper)
- Logs into `rtis.indianrail.gov.in`, extracts a bearer JWT (`accessToken`, from a hidden `#RTIS` input, with a localStorage/sessionStorage fallback) plus session cookies, then uses **`requests`, not Selenium**, for the actual per-loco polling loop — Selenium is only used for the login/CAPTCHA/OTP handshake.
- CAPTCHA solving is ddddocr-only with manual-admin fallback (no Google Vision tier here, unlike `login.py`).
- Only tracks locos returned by `/api/rtis/active_locos` (i.e., locos currently assigned to a non-relief crew whose loco_no didn't come strictly from CMS) — avoids wasting RTIS API calls on irrelevant locos.
- Per-loco polling calls `POST .../LiveDataForDivision`; on a location fix it computes `moved_gt_100m` via `haversine_distance()` against the previously stored lat/lon (100m threshold) before pushing to `/api/rtis/sync_loco` — this is what drives the app's departure-time auto-freeze logic.
- Faster cycle time than the CMS scraper: 12–18 min random sleep vs. CMS's 25–35 min, reflecting that loco position changes faster than duty/sign-on data.
- OTP is cached to `rtis_otp.txt` **only for the current calendar day** (checked via file mtime vs. `datetime.today().date()`), unlike the CMS scraper's undated reuse — assumes RTIS OTP requirements are day-scoped.

## Known Encoding Artifact
Several log/message strings contain mangled em-dash sequences (`ΓÇö`) — indicates the source file was saved/read with a Windows-1252-as-UTF-8 mismatch somewhere in the toolchain. Cosmetic only (affects console/log/UI text, not data), but worth fixing at the source if garbled text shows up in the admin UI.

## Decisions to Preserve
1. Do **not** consolidate the CMS/RTIS scraper supervisor loops into one — they have independent sentinel files and fatal-error states by design, so one portal's outage doesn't block the other.
2. `os._exit(1)` on disk-I/O/locked/readonly errors is intentional (not a bug) — it's the recovery mechanism for the free-tier VM's occasional storage hiccups; don't wrap and swallow these errors instead of restarting.
3. `DB_WRITE_LOCK` is Python-level, in addition to SQLite's own `busy_timeout` — keep both; removing the app-level lock re-introduces the original writer contention issue WAL mode + busy_timeout alone didn't fully solve.
4. 30-day retention window (submissions, duty log, soft-deleted crew records) is a deliberate compliance/storage tradeoff — don't extend without checking free-tier disk space.
5. Merge-priority order (admin > submission > CMS) must stay consistent across every place crew data is assembled — a regression here silently corrupts what officials see in the dashboard and reports.
6. Dual-tunnel SOCKS5 failover (Office PC 1080 → Laptop 1081) with a "block direct connection in production" rule is intentional, not overcautious — both CMS and RTIS block Oracle Cloud IPs directly, so a silent fallback to direct connection would just produce confusing failures instead of a clear "tunnels are down" signal.
7. The GCP Vision multi-account daily rotation exists specifically to stay under free-tier OCR quota across multiple projects — reducing to a single account will likely reintroduce quota exhaustion during high-CAPTCHA-failure periods.
8. The TA-sync dead-code fix (now running every valid-session cycle, not just post-login) is a confirmed bug fix, not a stylistic change — don't revert to the post-login-only trigger.
9. The CMS auth-failure circuit breaker (2 consecutive bad-password responses → fatal_error.txt → hard exit) is deliberate to avoid hammering the CMS login endpoint with retries on a stale/wrong password; don't loosen this without also adding a longer backoff.
10. Inside the `for row in data:` loop in `_get_crew_list_data()`, never use `continue` to skip a feature-specific sub-block (like the RTIS check) — it silently skips PDD and Duty Hours calculation for the rest of that row too. Use a boolean flag (e.g. `run_rtis_check`) and an `if:` block instead.
11. The PDD calculation block must stay at exactly 8-space indentation in `_get_crew_list_data()` — over-indenting nests it inside a preceding `if` and silently breaks it (this was the root cause of the "blank PDD" bug). Any refactor touching that function needs to verify PDD indentation explicitly.
12. The `is_rtis_departure` flag in `admin_edits` (and its deletion on manual `departure_time` edits in `/api/admin_edit`) must stay intact — it's what lets the frontend distinguish and style RTIS-auto-frozen departures vs. human-entered ones.
13. `crew_list.html`'s `updateDutyHours` JS requires `_sign_on_iso` to always be populated with a valid ISO string — any backend change to sign-on parsing must keep defaulting/populating this field or Duty Hours badges silently disappear with no error.
14. Keep using `.get('_duty_minutes', 0)` (not `['_duty_minutes']`) in `list_sort_key` and similar sort/access code — it's a deliberate defensive default against a row bypassing the calculation logic, not an oversight.
15. Both `otp.txt` (CMS) and `rtis_otp.txt` (RTIS) are now **day-scoped**: a cached OTP is only reused if the file's `mtime` matches today's calendar date (IST). Previously `login.py` had no date check and would silently reuse a stale OTP across midnight or process restarts. The two scrapers are now aligned on this pattern — don't revert to undated reuse.
16. `rtis_scraper.py` now has the **same 3-tier CAPTCHA cascade** as `login.py`: Tier 1 ddddocr (attempts 1–5), Tier 2 Google Cloud Vision with the same `GCP_CREDENTIALS` multi-account daily rotation (attempts 6–10), Tier 3 manual admin solve (>10 failures). The shared GCP credential loading (`_decode_gcp_b64`, `GCP_CREDENTIALS_B64_1..10` env vars, fallback to `gcp-creds.json`) is identical in both scrapers — any changes to the credential loading scheme must be mirrored in both files. Vision OCR usage is reported to `/api/ocr_usage` from both scrapers so the admin dashboard quota tracker stays accurate.