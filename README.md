# loan-portal-service

A small, self-contained FastAPI portal for **cooperative-bank staff** to verify
and redeem farmer micro-loan codes. It reads/writes the `loan_codes` table in the
dedicated `amul_loan` Postgres and keeps its own append-only audit log.

Modelled on `chat-export-service`: single-file FastAPI, HMAC-signed shared-login
cookie, server-rendered UI (no FE build step), docker-compose on an internal VM.
The UI is styled to resemble the Amul chat frontend (coral `#F65151` primary,
soft-pink background, Noto Sans, inlined Amul logo).

> **Internal-only.** Deploy behind the internal network with no public ingress
> (like chat-export on vm6). The shared login is only acceptable because the
> surface is internal. Do **not** expose this publicly with a shared password.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET  | `/login` | – | login page |
| POST | `/login` | – | password → HMAC session cookie |
| POST/GET | `/logout` | – | clear cookie |
| GET  | `/` | cookie | verify page (one input: 10-digit mobile **or** 6-digit code) |
| GET  | `/api/verify?phone=<10d>` | cookie | primary path — code(s) for a mobile (looser limit) |
| GET  | `/api/verify?code=<6d>` | cookie | single code lookup (**rate-limited**) |
| POST | `/api/redeem` | cookie | `{code, nonce}` → mark redeemed (idempotent) |
| GET  | `/api/export.csv?from=&to=` | cookie | redeemed rows in a date range (audited) |
| GET  | `/healthz` | – | liveness |

### Admin area (separate login)

A second, more-powerful surface for managing the eligibility list and loan codes.
It has its **own password and its own cookie/scope** — a verify session can never
reach it, and an admin session grants no verify/redeem rights (see below).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET  | `/admin/login` | – | admin login page |
| POST | `/admin/login` | – | `ADMIN_PASSWORD` → `admin_session` cookie |
| POST/GET | `/admin/logout` | – | clear admin cookie |
| GET  | `/admin` | admin cookie | admin dashboard (upload / list / codes) |
| POST | `/admin/eligibility/upload` | admin cookie | multipart `file` (.csv/.xlsx) + optional `tag` → upsert eligibility |
| GET  | `/admin/api/eligibility?q=<phone-or-name>` | admin cookie | search/list eligibility rows (≤200) |
| POST | `/admin/eligibility/remove` | admin cookie | JSON `{phone}` → **hard delete** all of that phone's rows |
| GET  | `/admin/api/codes?phone=<10d>` | admin cookie | that phone's codes + statuses |
| GET  | `/admin/api/issued?status=&q=&from=&to=&limit=` | admin cookie | **every** farmer sent a code, newest first |
| GET  | `/admin/issued.csv?status=&q=&from=&to=` | admin cookie | the same list as CSV (uncapped, audited) |
| POST | `/admin/codes/clear` | admin cookie | JSON `{phone}` → **cancel** active codes (record kept) |

**Auth separation.** The admin cookie is `admin_session` (verify uses
`lps_session`) and its HMAC payload folds in an `admin` scope marker
(`HMAC(SESSION_SECRET, "admin|<exp>")`) whereas the verify cookie signs just
`"<exp>"`. So even if someone copied a verify token into the `admin_session`
cookie the MAC would not validate — and vice-versa. Both the name **and** the
signed scope differ (belt-and-suspenders). `SESSION_SECRET` is reused, but the
scope marker keeps the two token families disjoint.

**Add (upload).** `POST /admin/eligibility/upload` accepts a `.csv` (stdlib
`csv`) or `.xlsx` (`openpyxl`) using the **same SABHSAD column mapping and
last-10-digits phone normalization** as `backend/scripts/load_sabhsad.py`
(`CODE`→farmer_code, `AC NO`→ac_no, `SABHSAD NAME`→sabhsad_name,
`MILK PAYMENT AMOUNT`→milk_payment_amount, `MANDALI NAME`→mandali_name,
`MOBILE NO`→phone). Each row upserts on `(phone, farmer_code)`
(`ON CONFLICT … DO UPDATE`, `is_active=true`, `source_batch=admin-upload-<tag-or-UTC-timestamp>`).
Rows with a missing/short phone are **skipped** (not fatal); blank spacer rows are
ignored. The upload is size-capped (`ADMIN_UPLOAD_MAX_BYTES`, default 5 MiB) and
sniffed (`.xlsx` must be a `PK…` zip). The response reports
`{parsed, upserted, skipped, skipped_detail[]}`.

**Per-farmer loan amount.** The sheet carries a `MAX LOAN AMOUNT` column
(`loan_eligibility_list.max_loan_amount`). It is **the amount the farmer is
offered in chat/voice and the figure in their approval SMS**, so the parser
rejects a row whose amount is non-numeric, zero or negative, or above
`MAX_LOAN_AMOUNT_LIMIT` when that is set — the row is named in `skipped_detail`
rather than loaded. Blank is legitimate and means "standard amount": it loads as
NULL and the backends fall back to their `LOAN_MAX_AMOUNT`. Sheets predating the
column keep loading unchanged. The upload and the quick-add share one validator,
so a limit cannot be side-stepped by adding a number by hand.

**Who has been sent a code.** `GET /admin/api/issued` lists across all farmers,
filtered by `status` (`all|active|redeemed|cancelled|expired`), free text `q`
(phone / code / name / farmer code / mandali), and an inclusive `from`/`to`
issued-on range. `expired` and `active` are filtered on the *derived* state (a
lapsed code keeps `status='active'` until the sweep runs), matching
`effective_status`. The response carries both `count` (rows returned, ≤ `limit`,
default 200) and `total` (rows matched) so a truncated page is never mistaken for
the whole set. `GET /admin/issued.csv` returns the same filter **uncapped** —
truncating a reconciliation file would corrupt it — and writes an
`action='admin_export'` audit row, since it carries full phone numbers.

**Remove.** `POST /admin/eligibility/remove` hard-deletes every
`loan_eligibility_list` row for a phone and reports the count.

**Clear codes (regenerate).** `POST /admin/codes/clear` sets
`status='cancelled', updated_at=now()` on that phone's **active** codes (it does
**not** delete). Cancelling is enough to reset the already-availed guard, so the
farmer can regenerate a fresh code through the normal chat/voice flow. Reports
how many were cancelled.

**Audit.** Admin actions reuse `loan_redemption_audit` with
`actor=ADMIN_ACTOR` (default `admin:shared`) and `action` ∈
`admin_upload | admin_remove | admin_clear` (all fit the `VARCHAR(16)` column),
with counts/detail in the JSONB `detail`. A dedicated admin-audit table could
follow later if the shared vocabulary becomes awkward.

`verify` returns per code: `code`, masked `phone_masked` (full phone stays
server-side), `farmer_name`, `farmer_code`, `mandali_name`, `union_code`,
`loan_amount`, `status`, `issued_at`, `expires_at`, `redeemed_at`, `redeemed_by`,
and a derived **`effective_status`** (an `active` row past `expires_at` is
reported as `expired` live, even before the sweep flips it).

## How it works

### Redeem (the core write, idempotent)
One transaction with a load-bearing guard:

```sql
UPDATE loan_codes
   SET status='redeemed', redeemed_at=now(), redeemed_by=:actor, updated_at=now()
 WHERE code=:code AND status='active' AND (expires_at IS NULL OR expires_at > now());
```

- `redeemed_by` = `PORTAL_ACTOR` (`portal:shared` in MVP).
- The `status='active'` WHERE clause is optimistic concurrency for free: a
  double-submit / two-staff race updates **0 rows** the second time. On 0 rows
  the endpoint re-reads the row and returns its current state + a clear
  "already redeemed / expired / cancelled / not found" message — **not** an error.
- Every call writes an audit row (`redeem` on hit, `redeem_noop` on miss).

### Rate limiting (code endpoint)
Driven off the audit table (no in-memory state, survives restarts):
- ≤ `CODE_LOOKUPS_PER_SESSION` (default **5**) code lookups per session, and
- ≤ `CODE_LOOKUPS_PER_IP` (default **20**) per source IP,
- within a trailing `RATE_WINDOW_MIN` (default **10 min**) window.

On exceed → HTTP **429** and an audit row with `result='rate_limited'`. Mobile
lookups are the promoted path and are not code-rate-limited.

### Redeem nonce (verify → redeem binding)
A successful verify attaches a short-lived **server-signed nonce**
(`HMAC(session_id | code | exp)`, TTL `NONCE_TTL`, default 5 min) to each
live-redeemable code. `POST /api/redeem` requires a valid nonce for that exact
code + session, so a *guessed* code cannot be redeemed without first passing the
(rate-limited) verify. The nonce can't be forged without `SESSION_SECRET`.

### Audit / PII
Every verify/redeem/export writes to `loan_redemption_audit` (`at`, `actor`,
`action`, `lookup_kind`, `code`, `phone`, `result`, `src_ip`, `detail` jsonb).
This is the controlled record — full phone/code are never written to stdout; the
UI only ever sees masked phones (e.g. `98••••34`).

## Environment (`.env`)

| Var | Default | Notes |
|---|---|---|
| `PORTAL_PASSWORD` | – (required) | shared login; the only place it lives |
| `SESSION_SECRET` | derived from password | set explicitly so sessions survive password rotation |
| `SESSION_HOURS` | `8` | cookie lifetime |
| `COOKIE_SECURE` | `true` | set `false` only for plain-http local dev |
| `PORTAL_ACTOR` | `portal:shared` | `redeemed_by` / audit actor |
| `ADMIN_PASSWORD` | – (required for admin) | **separate** admin login; distinct from `PORTAL_PASSWORD`. Make it strong. |
| `ADMIN_ACTOR` | `admin:shared` | audit actor for admin actions |
| `ADMIN_UPLOAD_MAX_BYTES` | `5242880` | max eligibility upload size (5 MiB) |
| `MAX_LOAN_AMOUNT_LIMIT` | – (unset) | ceiling on a per-farmer `MAX LOAN AMOUNT`. Unset = no check. Set it and a sheet row above it is rejected at import, so a misplaced decimal cannot become an approved loan. Read at startup — restart after changing. |
| `LOAN_DB_URL` | – (required) | `postgresql+asyncpg://amul_loan:…@host:5432/amul_loan` |
| `WEB_PORT` | `8085` | host port |
| `CODE_LOOKUPS_PER_SESSION` / `CODE_LOOKUPS_PER_IP` / `RATE_WINDOW_MIN` / `NONCE_TTL` | 5 / 20 / 10 / 300 | rate-limit + nonce knobs |

## Tests

There is no CI on this repo — run them yourself before deploying:

```bash
pip install -r requirements-dev.txt && pytest
```

They cover the pure logic that decides loan amounts and filters the issued list
(sheet parsing, the amount validator and its limit, the template round-trip, and
the issued-code WHERE builder). No Postgres or network needed.

## Run locally

```bash
cp .env.example .env          # fill PORTAL_PASSWORD, LOAN_DB_URL; COOKIE_SECURE=false for http
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8085
# open http://localhost:8085
```

The DB engine is lazy — the app imports and starts without a live DB; it only
connects on the first verify/redeem/export call.

## Deploy

1. Apply the migrations once against the `amul_loan` DB:
   ```bash
   psql "$LOAN_DB_DSN" -f migrations/002_audit.sql
   psql "$LOAN_DB_DSN" -f migrations/003_max_loan_amount.sql
   ```
   `003` is additive and nullable, so apply it **before** rolling out this build
   and the matching `amul-oan-api` / `voice-oan-api` builds; the running versions
   ignore the new column. The same DDL also ships as those repos'
   `migrations/loan/002` — both are `ADD COLUMN IF NOT EXISTS` against the one
   shared database, so whichever runs first wins and the rest are no-ops.
   (`loan_codes` already exists from `amul-oan-api/migrations/loan/001_init.sql`
   — the portal makes **no** change to it.)
2. Fill `.env` (keep `COOKIE_SECURE=true`), then:
   ```bash
   docker compose up -d --build
   ```
3. The `web` service listens on `${WEB_PORT:-8085}` on the internal VM. Front it
   only on the internal network — no public ingress.

**Compose network:** joins the external `amul-network` (as the other amul
services do). Create it once if missing: `docker network create amul-network`.
If the `amul_loan` DB is reached by host IP instead of the compose network, the
network attachment is harmless.

## Security notes

- Internal-only exposure is the primary control; shared login only acceptable there.
- 6-digit codes are brute-forceable → mobile lookup promoted, code endpoint hard
  rate-limited, redeem bound to a prior verify via nonce.
- PII minimization: masked phone in the **bank portal** UI, full PII only
  server-side, never in logs. The **admin** surface deliberately shows full
  phones (it manages the eligibility list keyed on them) and `/admin/issued.csv`
  exports them — that download is audited, and is a reason the admin area needs
  the IP-allowlist / Keycloak hardening noted below.
- Cookie: `HttpOnly`, `SameSite=Lax`, `Secure` (toggle `COOKIE_SECURE` for dev),
  short lifetime; set `SESSION_SECRET` so the cookie survives password rotation.
- **Admin surface is powerful** — bulk eligibility upload/remove and code
  cancellation. The portal is now **publicly routed** at
  `loan-portal.dev.amulai.in`, so the shared-internal assumption no longer holds
  for the admin area. **Set a strong, distinct `ADMIN_PASSWORD` now**, and put an
  **IP-allowlist and/or Keycloak** in front of `/admin/*` as the next hardening
  step (the audit `actor` column is already Keycloak-ready — see v2 below).

## v2 — Keycloak swap

The `amul` realm already exists. To move off the shared login: add a
`loan-portal` client + `loan-verifier` realm role, port a JWKS bearer middleware
to Python, and set `PORTAL_ACTOR` / `redeemed_by` from the token
`preferred_username`. Because `redeemed_by` and the audit `actor` column are
already in place, this is a swap, not a migration. Per-branch/union scoping then
rides on a Keycloak claim against the existing `union_code`/`society_code`
columns.
