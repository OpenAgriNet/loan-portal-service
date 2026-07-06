#!/usr/bin/env python3
"""Bank-side micro-loan verification & redemption portal.

A tiny, self-contained FastAPI app for cooperative-bank staff to look up a
farmer's loan code (by mobile or by 6-digit code), see its status, and mark it
redeemed. Modelled on ``chat-export-service`` (single-file FastAPI, HMAC-signed
shared-login cookie, docker-compose on an internal VM).

Security model (see backend/LOAN_PORTAL_DESIGN.md §5):
- Single shared login held ONLY in env (PORTAL_PASSWORD) -> HMAC session cookie.
- Mobile lookup is the promoted primary path; the 6-digit code endpoint is
  hard rate-limited (codes are brute-forceable).
- Redeem is bound to a prior successful verify in the same session via a
  short-lived server-signed nonce, so a guessed code can't be redeemed without
  first passing the (rate-limited) verify.
- Every verify/redeem writes an append-only audit row; full PII is never logged
  to stdout, only masked values reach the UI.

The portal only ever writes status/redeemed_at/redeemed_by/updated_at on
``loan_codes``; it never issues codes or touches PII.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, File, Form, Query, Request, HTTPException, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ---------------- config (env only; no secrets in code) ----------------
PASSWORD = os.environ.get("PORTAL_PASSWORD", "")
SESSION_SECRET = (os.environ.get("SESSION_SECRET") or ("sk:" + PASSWORD)).encode()
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "8"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() not in ("0", "false", "no")
# actor recorded on redeem + audit. Module constant / env so the v2 Keycloak
# swap (redeemed_by = token username) is a one-line change.
PORTAL_ACTOR = os.environ.get("PORTAL_ACTOR", "portal:shared")
LOAN_DB_URL = os.environ.get("LOAN_DB_URL", "")

COOKIE = "lps_session"

# ---------------- admin config (separate, independent of the verify login) ----
# The admin surface (bulk eligibility upload/remove + code cancellation) is a
# distinct, more-powerful capability. It has its OWN password and its OWN cookie
# with a distinct HMAC scope marker, so a verify-session cookie can never
# authorize admin and vice-versa (see _admin_sign/_admin_valid below).
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_COOKIE = "admin_session"
ADMIN_SCOPE = "admin"  # signed into the admin cookie's HMAC payload; NOT in verify cookies
ADMIN_ACTOR = os.environ.get("ADMIN_ACTOR", "admin:shared")
# Cap for uploaded eligibility files (default 5 MiB) — guards memory + abuse.
ADMIN_UPLOAD_MAX_BYTES = int(os.environ.get("ADMIN_UPLOAD_MAX_BYTES", str(5 * 1024 * 1024)))

# Rate-limit knobs for the code endpoint (mobile lookups are looser).
CODE_LOOKUPS_PER_SESSION = int(os.environ.get("CODE_LOOKUPS_PER_SESSION", "5"))
CODE_LOOKUPS_PER_IP = int(os.environ.get("CODE_LOOKUPS_PER_IP", "20"))
RATE_WINDOW_MIN = int(os.environ.get("RATE_WINDOW_MIN", "10"))
# Redeem nonce lifetime (seconds) — short: the staffer verifies then redeems.
NONCE_TTL = int(os.environ.get("NONCE_TTL", "300"))

PHONE_RE = re.compile(r"^\d{10}$")
CODE_RE = re.compile(r"^\d{6}$")

app = FastAPI(title="Amul loan portal", docs_url=None, redoc_url=None)


# ---------------- lazy async DB (no connection at import) ----------------
_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker] = None


def _get_sessionmaker() -> async_sessionmaker:
    global _engine, _sessionmaker
    if _sessionmaker is not None:
        return _sessionmaker
    if not LOAN_DB_URL:
        raise RuntimeError("LOAN_DB_URL is not configured")
    _engine = create_async_engine(LOAN_DB_URL, pool_pre_ping=True, future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _sessionmaker


# ---------------- session cookie (stateless, HMAC-signed) ----------------
def _sign(exp: int) -> str:
    mac = hmac.new(SESSION_SECRET, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def _valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, mac = token.rsplit(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    good = hmac.new(SESSION_SECRET, exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, good)


def _authed(request: Request) -> bool:
    return _valid(request.cookies.get(COOKIE))


def _require_auth(request: Request) -> None:
    if not _authed(request):
        raise HTTPException(status_code=401, detail="not authenticated")


def _session_id(request: Request) -> str:
    """Stable per-session id derived from the signed cookie (used for
    per-session rate-limit accounting and nonce binding)."""
    tok = request.cookies.get(COOKIE) or ""
    return hashlib.sha256((SESSION_SECRET + tok.encode())).hexdigest()[:24]


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------- admin session cookie (separate scope from verify) ----------
# Signed exactly like the verify cookie but with the ADMIN_SCOPE folded into the
# HMAC payload. Consequences:
#   * A verify cookie (payload = "<exp>") recomputes a DIFFERENT MAC than an
#     admin cookie (payload = "admin|<exp>"), so copying a verify token into the
#     admin_session cookie fails _admin_valid — and the reverse fails _valid.
#   * The two also use different cookie NAMES, so the scopes never cross even
#     before the MAC check. This is belt-and-suspenders on purpose.
def _admin_sign(exp: int) -> str:
    payload = f"{ADMIN_SCOPE}|{exp}"
    mac = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def _admin_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, mac = token.rsplit(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    good = hmac.new(SESSION_SECRET, f"{ADMIN_SCOPE}|{exp_s}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, good)


def _admin_authed(request: Request) -> bool:
    return _admin_valid(request.cookies.get(ADMIN_COOKIE))


def _require_admin(request: Request) -> None:
    # A normal verify session must NOT satisfy this: we only ever read the
    # admin_session cookie, and it must carry the admin-scoped MAC.
    if not _admin_authed(request):
        raise HTTPException(status_code=401, detail="not authenticated (admin)")


# ---------------- redeem nonce (server-signed, verify -> redeem binding) ----
def _nonce_for(session_id: str, code: str, exp: int) -> str:
    """HMAC(session_id|code|exp). Ties a redeem to a code the same session just
    verified, within NONCE_TTL. Can't be forged without SESSION_SECRET."""
    payload = f"{session_id}|{code}|{exp}"
    mac = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def _issue_nonce(session_id: str, code: str) -> str:
    exp = int(time.time()) + NONCE_TTL
    return _nonce_for(session_id, code, exp)


def _nonce_ok(session_id: str, code: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, _mac = token.rsplit(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    good = _nonce_for(session_id, code, exp)
    return hmac.compare_digest(token, good)


# ---------------- helpers ----------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 4:
        return "•" * len(digits)
    return f"{digits[:2]}{'•' * (len(digits) - 4)}{digits[-2:]}"


def _iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _effective_status(status: str, expires_at: Any) -> str:
    """Compute expiry live: an 'active' row whose expires_at has passed is
    treated as expired even if the sweep hasn't flipped it yet."""
    if status == "active" and expires_at is not None:
        try:
            exp = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < _now():
                return "expired"
        except (ValueError, TypeError):
            pass
    return status


_CODE_COLS = (
    "code, phone, farmer_name, farmer_code, mandali_name, union_code, "
    "society_code, loan_amount, status, issued_at, expires_at, redeemed_at, "
    "redeemed_by"
)


def _row_to_public(row: Any, session_id: str) -> dict:
    """Shape a loan_codes row for the client. Phone is masked; a fresh redeem
    nonce is attached when the code is live-redeemable."""
    d = dict(row._mapping)
    eff = _effective_status(d["status"], d.get("expires_at"))
    out = {
        "code": d["code"],
        "phone_masked": _mask_phone(d.get("phone")),
        "farmer_name": d.get("farmer_name"),
        "farmer_code": d.get("farmer_code"),
        "mandali_name": d.get("mandali_name"),
        "union_code": d.get("union_code"),
        "loan_amount": float(d["loan_amount"]) if d.get("loan_amount") is not None else None,
        "status": d.get("status"),
        "effective_status": eff,
        "issued_at": _iso(d.get("issued_at")),
        "expires_at": _iso(d.get("expires_at")),
        "redeemed_at": _iso(d.get("redeemed_at")),
        "redeemed_by": d.get("redeemed_by"),
    }
    if eff == "active":
        out["redeem_nonce"] = _issue_nonce(session_id, d["code"])
    return out


# ---------------- SABHSAD eligibility parsing (admin upload) ----------------
# Column mapping + phone normalization are kept identical to the ops loader
# backend/scripts/load_sabhsad.py so a file loaded either way behaves the same.
_SABHSAD_HEADER_MAP = {
    "code": "farmer_code",
    "ac no": "ac_no",
    "sabhsad name": "sabhsad_name",
    "milk payment amount": "milk_payment_amount",
    "mandali name": "mandali_name",
    "mobile no": "phone",
}


def _norm_phone(value: Any) -> Optional[str]:
    """Last-10-digits normalization — same key the app matches eligibility on."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def _clean_cell(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _num_str(value: Any) -> Optional[str]:
    """Return a numeric-looking string suitable for CAST(... AS NUMERIC), else
    None. Kept as a string so the value binds cleanly via CAST (no driver-side
    Decimal coercion on the async path)."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        float(s)
    except ValueError:
        return None
    return s


def _read_csv_rows(data: bytes) -> list[list]:
    text_data = data.decode("utf-8-sig", errors="replace")
    return [row for row in csv.reader(io.StringIO(text_data))]


def _read_xlsx_rows(data: bytes) -> list[list]:
    import openpyxl  # lazy: keeps import-time light and csv-only path dep-free

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _parse_eligibility_bytes(
    filename: str, data: bytes, *, source_batch: str
) -> tuple[list[dict], list[dict]]:
    """Pure (no DB) parse of a SABHSAD .csv/.xlsx payload into eligibility rows.

    Returns ``(rows, skipped)`` where ``rows`` are upsert-ready dicts and
    ``skipped`` is ``[{"row": <1-based>, "reason": ...}]``. Raises ValueError on
    an unsupported type or a sheet missing the required columns."""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        table = _read_xlsx_rows(data)
    elif name.endswith(".csv"):
        table = _read_csv_rows(data)
    else:
        raise ValueError("unsupported file type; upload a .csv or .xlsx")

    if not table:
        return [], []
    header = [(_clean_cell(h) or "").lower() for h in table[0]]
    col_idx = {_SABHSAD_HEADER_MAP[h]: i for i, h in enumerate(header) if h in _SABHSAD_HEADER_MAP}
    missing = {"phone", "farmer_code"} - set(col_idx)
    if missing:
        raise ValueError(f"required column(s) not found: {sorted(missing)} (headers={header})")

    def _at(row: list, key: str) -> Any:
        i = col_idx.get(key)
        return row[i] if (i is not None and i < len(row)) else None

    rows: list[dict] = []
    skipped: list[dict] = []
    for n, r in enumerate(table[1:], start=2):
        if all(_clean_cell(c) is None for c in r):
            continue  # blank spacer row — not counted as parsed/skipped
        phone = _norm_phone(_at(r, "phone"))
        if not phone:
            skipped.append({"row": n, "reason": "missing or short phone (need >=10 digits)"})
            continue
        rows.append({
            "phone": phone,
            "farmer_code": _clean_cell(_at(r, "farmer_code")),
            "mandali_name": _clean_cell(_at(r, "mandali_name")),
            "sabhsad_name": _clean_cell(_at(r, "sabhsad_name")),
            "ac_no": _clean_cell(_at(r, "ac_no")),
            "milk_payment_amount": _num_str(_at(r, "milk_payment_amount")),
            "source_batch": source_batch,
        })
    return rows, skipped


# Upsert on (phone, farmer_code); flips is_active back to true on re-add.
_ELIG_UPSERT_SQL = (
    "INSERT INTO loan_eligibility_list "
    "(phone, farmer_code, mandali_name, sabhsad_name, ac_no, milk_payment_amount, "
    " source_batch, is_active) "
    "VALUES (:phone, :farmer_code, :mandali_name, :sabhsad_name, :ac_no, "
    " CAST(:milk_payment_amount AS NUMERIC), :source_batch, true) "
    "ON CONFLICT ON CONSTRAINT uq_loan_eligibility_phone_farmer DO UPDATE SET "
    "  mandali_name = EXCLUDED.mandali_name, "
    "  sabhsad_name = EXCLUDED.sabhsad_name, "
    "  ac_no = EXCLUDED.ac_no, "
    "  milk_payment_amount = EXCLUDED.milk_payment_amount, "
    "  source_batch = EXCLUDED.source_batch, "
    "  is_active = true"
)


# ---------------- audit ----------------
async def _audit(
    sess: AsyncSession,
    *,
    actor: str,
    action: str,
    result: str,
    lookup_kind: Optional[str] = None,
    code: Optional[str] = None,
    phone: Optional[str] = None,
    src_ip: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    """Append one row to loan_redemption_audit. This is the controlled record —
    the only place full phone/code are persisted for the portal's own actions."""
    await sess.execute(
        text(
            "INSERT INTO loan_redemption_audit "
            "(actor, action, lookup_kind, code, phone, result, src_ip, detail) "
            "VALUES (:actor, :action, :lookup_kind, :code, :phone, :result, "
            ":src_ip, CAST(:detail AS JSONB))"
        ),
        {
            "actor": actor,
            "action": action,
            "lookup_kind": lookup_kind,
            "code": code,
            "phone": phone,
            "result": result,
            "src_ip": src_ip,
            "detail": json.dumps(detail) if detail is not None else None,
        },
    )


async def _rate_limited(sess: AsyncSession, session_id: str, src_ip: str) -> bool:
    """True when the code-lookup budget for this session or IP is exhausted in
    the trailing window. Driven off the audit table (action='verify',
    lookup_kind='code')."""
    row = (
        await sess.execute(
            text(
                "SELECT "
                "  count(*) FILTER (WHERE (detail->>'session_id') = :sid) AS by_session, "
                "  count(*) FILTER (WHERE src_ip = :ip) AS by_ip "
                "FROM loan_redemption_audit "
                "WHERE action = 'verify' AND lookup_kind = 'code' "
                "  AND at > now() - make_interval(mins => :win)"
            ),
            {"sid": session_id, "ip": src_ip, "win": RATE_WINDOW_MIN},
        )
    ).first()
    by_session = row.by_session if row else 0
    by_ip = row.by_ip if row else 0
    return by_session >= CODE_LOOKUPS_PER_SESSION or by_ip >= CODE_LOOKUPS_PER_IP


# =======================================================================
# Routes
# =======================================================================
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# ---------------- auth ----------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _authed(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(render_login(""))


@app.post("/login")
def login(password: str = Form("")):
    if not PASSWORD:
        return PlainTextResponse("Server has no PORTAL_PASSWORD configured.", status_code=500)
    if not hmac.compare_digest(password, PASSWORD):
        return HTMLResponse(render_login("Incorrect password."), status_code=401)
    exp = int(time.time()) + SESSION_HOURS * 3600
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE, _sign(exp), httponly=True, samesite="lax",
        secure=COOKIE_SECURE, max_age=SESSION_HOURS * 3600,
    )
    return resp


@app.post("/logout")
@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------- verify page (server-rendered) ----------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(render_portal())


# ---------------- verify API ----------------
@app.get("/api/verify")
async def api_verify(
    request: Request,
    phone: Optional[str] = Query(None),
    code: Optional[str] = Query(None),
):
    _require_auth(request)
    session_id = _session_id(request)
    src_ip = _client_ip(request)

    phone = (phone or "").strip()
    code = (code or "").strip()

    if bool(phone) == bool(code):
        raise HTTPException(status_code=400, detail="provide exactly one of phone or code")
    # Validate format BEFORE opening a DB session so malformed input never
    # reaches (or requires) the database.
    if code and not CODE_RE.match(code):
        raise HTTPException(status_code=400, detail="code must be 6 digits")
    if phone and not PHONE_RE.match(phone):
        raise HTTPException(status_code=400, detail="mobile must be 10 digits")

    sm = _get_sessionmaker()
    async with sm() as sess:
        # ----- code path: rate-limited -----
        if code:
            if await _rate_limited(sess, session_id, src_ip):
                await _audit(
                    sess, actor=PORTAL_ACTOR, action="verify", lookup_kind="code",
                    code=code, result="rate_limited", src_ip=src_ip,
                    detail={"session_id": session_id},
                )
                await sess.commit()
                return JSONResponse(
                    {"error": "rate_limited",
                     "message": "Too many code lookups. Please wait a few minutes or search by mobile number."},
                    status_code=429,
                )
            res = await sess.execute(
                text(f"SELECT {_CODE_COLS} FROM loan_codes WHERE code = :code"),
                {"code": code},
            )
            row = res.first()
            result = "ok" if row else "not_found"
            await _audit(
                sess, actor=PORTAL_ACTOR, action="verify", lookup_kind="code",
                code=code, phone=(dict(row._mapping).get("phone") if row else None),
                result=result, src_ip=src_ip, detail={"session_id": session_id},
            )
            await sess.commit()
            codes = [_row_to_public(row, session_id)] if row else []
            return {"lookup": "code", "count": len(codes), "codes": codes}

        # ----- phone path: primary, looser -----
        res = await sess.execute(
            text(
                f"SELECT {_CODE_COLS} FROM loan_codes WHERE phone = :phone "
                "ORDER BY (status='active') DESC, issued_at DESC"
            ),
            {"phone": phone},
        )
        rows = res.fetchall()
        await _audit(
            sess, actor=PORTAL_ACTOR, action="verify", lookup_kind="phone",
            phone=phone, result=("ok" if rows else "not_found"), src_ip=src_ip,
            detail={"session_id": session_id, "count": len(rows)},
        )
        await sess.commit()
        codes = [_row_to_public(r, session_id) for r in rows]
        return {"lookup": "phone", "count": len(codes), "codes": codes}


# ---------------- redeem API ----------------
@app.post("/api/redeem")
async def api_redeem(request: Request):
    _require_auth(request)
    session_id = _session_id(request)
    src_ip = _client_ip(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    code = str((body or {}).get("code", "")).strip()
    nonce = (body or {}).get("nonce")
    if not CODE_RE.match(code):
        raise HTTPException(status_code=400, detail="code must be 6 digits")

    # Redeem must be bound to a prior successful verify in this session.
    if not _nonce_ok(session_id, code, nonce):
        raise HTTPException(
            status_code=403,
            detail="verify the code before redeeming (missing or expired verification)",
        )

    sm = _get_sessionmaker()
    async with sm() as sess:
        # The status='active' + live-expiry guard in the WHERE clause is
        # load-bearing: it gives optimistic-concurrency for free (a double
        # submit / two-staff race updates 0 rows the second time).
        upd = await sess.execute(
            text(
                "UPDATE loan_codes "
                "SET status='redeemed', redeemed_at=now(), redeemed_by=:actor, "
                "    updated_at=now() "
                "WHERE code=:code AND status='active' "
                "  AND (expires_at IS NULL OR expires_at > now()) "
                "RETURNING code"
            ),
            {"code": code, "actor": PORTAL_ACTOR},
        )
        hit = upd.first() is not None

        # Re-read the current row for the response either way (idempotent).
        row = (
            await sess.execute(
                text(f"SELECT {_CODE_COLS} FROM loan_codes WHERE code = :code"),
                {"code": code},
            )
        ).first()

        if hit:
            await _audit(
                sess, actor=PORTAL_ACTOR, action="redeem", lookup_kind="code",
                code=code, phone=(dict(row._mapping).get("phone") if row else None),
                result="ok", src_ip=src_ip, detail={"session_id": session_id},
            )
            await sess.commit()
            return {
                "redeemed": True,
                "message": "Loan code issued.",
                "code": _row_to_public(row, session_id) if row else None,
            }

        # 0 rows updated: determine why and return current state (not an error).
        if row is None:
            result, message = "not_found", "Code not found."
            current = None
        else:
            d = dict(row._mapping)
            eff = _effective_status(d["status"], d.get("expires_at"))
            current = _row_to_public(row, session_id)
            if d["status"] == "redeemed":
                result = "already"
                when = _iso(d.get("redeemed_at")) or "earlier"
                by = d.get("redeemed_by") or "portal"
                message = f"Already issued on {when} by {by}."
            elif eff == "expired" or d["status"] == "expired":
                result, message = "expired", "This code has expired."
            elif d["status"] == "cancelled":
                result, message = "cancelled", "This code was cancelled."
            else:
                result, message = "already", "Code is not active."
        await _audit(
            sess, actor=PORTAL_ACTOR, action="redeem_noop", lookup_kind="code",
            code=code, phone=(dict(row._mapping).get("phone") if row else None),
            result=result, src_ip=src_ip, detail={"session_id": session_id},
        )
        await sess.commit()
        return {"redeemed": False, "reason": result, "message": message, "code": current}


# ---------------- CSV export (reconciliation) ----------------
@app.get("/api/export.csv")
async def api_export(
    request: Request,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    _require_auth(request)
    src_ip = _client_ip(request)
    session_id = _session_id(request)
    try:
        d_from = datetime.fromisoformat(from_)
        d_to = datetime.fromisoformat(to)
    except ValueError:
        raise HTTPException(status_code=400, detail="from/to must be ISO dates (YYYY-MM-DD)")

    sm = _get_sessionmaker()
    async with sm() as sess:
        rows = (
            await sess.execute(
                text(
                    "SELECT code, phone, farmer_name, farmer_code, mandali_name, "
                    "union_code, society_code, loan_amount, status, issued_at, "
                    "redeemed_at, redeemed_by "
                    "FROM loan_codes "
                    "WHERE status='redeemed' AND redeemed_at >= :f AND redeemed_at < :t "
                    "ORDER BY redeemed_at"
                ),
                {"f": d_from, "t": d_to},
            )
        ).fetchall()
        await _audit(
            sess, actor=PORTAL_ACTOR, action="verify", lookup_kind=None,
            result="ok", src_ip=src_ip,
            detail={"session_id": session_id, "export": True,
                    "from": from_, "to": to, "count": len(rows)},
        )
        await sess.commit()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "code", "phone", "farmer_name", "farmer_code", "mandali_name",
        "union_code", "society_code", "loan_amount", "status", "issued_at",
        "redeemed_at", "redeemed_by",
    ])
    for r in rows:
        m = dict(r._mapping)
        w.writerow([
            m["code"], m["phone"], m.get("farmer_name"), m.get("farmer_code"),
            m.get("mandali_name"), m.get("union_code"), m.get("society_code"),
            m.get("loan_amount"), m.get("status"), _iso(m.get("issued_at")),
            _iso(m.get("redeemed_at")), m.get("redeemed_by"),
        ])
    filename = f"redemptions_{from_}_to_{to}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# =======================================================================
# Admin area — SEPARATE login, SEPARATE cookie/scope. Additive only; the
# verify/redeem routes above are untouched.
#
# Admin actions reuse the existing loan_redemption_audit table with action
# values 'admin_upload' | 'admin_remove' | 'admin_clear' (all fit VARCHAR(16)).
# These extend the audit vocabulary; a dedicated admin-audit table could follow
# later if the shared table becomes awkward.
# =======================================================================
@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if _admin_authed(request):
        return RedirectResponse("/admin", status_code=303)
    return HTMLResponse(render_admin_login(""))


@app.post("/admin/login")
def admin_login(password: str = Form("")):
    if not ADMIN_PASSWORD:
        return PlainTextResponse("Server has no ADMIN_PASSWORD configured.", status_code=500)
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        return HTMLResponse(render_admin_login("Incorrect password."), status_code=401)
    exp = int(time.time()) + SESSION_HOURS * 3600
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        ADMIN_COOKIE, _admin_sign(exp), httponly=True, samesite="lax",
        secure=COOKIE_SECURE, max_age=SESSION_HOURS * 3600,
    )
    return resp


@app.post("/admin/logout")
@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@app.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request):
    if not _admin_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    return HTMLResponse(render_admin())


# ---------------- eligibility: upload (add) ----------------
@app.post("/admin/eligibility/upload")
async def admin_eligibility_upload(
    request: Request,
    file: UploadFile = File(...),
    tag: str = Form(""),
):
    _require_admin(request)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > ADMIN_UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large (max {ADMIN_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB)",
        )
    name = (file.filename or "").lower()
    if not (name.endswith(".csv") or name.endswith(".xlsx")):
        raise HTTPException(status_code=400, detail="upload a .csv or .xlsx file")
    # Content sniff: .xlsx is a zip (magic 'PK'); reject a mislabelled blob.
    if name.endswith(".xlsx") and not data[:2] == b"PK":
        raise HTTPException(status_code=400, detail="file is not a valid .xlsx (zip) container")

    clean_tag = re.sub(r"[^A-Za-z0-9_.-]", "", (tag or "").strip())[:32]
    batch = f"admin-upload-{clean_tag or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    try:
        rows, skipped = _parse_eligibility_bytes(file.filename or name, data, source_batch=batch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=400, detail="could not parse file (corrupt or unexpected format)")

    parsed = len(rows) + len(skipped)
    sm = _get_sessionmaker()
    async with sm() as sess:
        upserted = 0
        for rec in rows:
            await sess.execute(text(_ELIG_UPSERT_SQL), rec)
            upserted += 1
        await _audit(
            sess, actor=ADMIN_ACTOR, action="admin_upload", result="ok",
            src_ip=_client_ip(request),
            detail={"batch": batch, "filename": file.filename,
                    "parsed": parsed, "upserted": upserted, "skipped": len(skipped)},
        )
        await sess.commit()

    return {
        "ok": True,
        "batch": batch,
        "parsed": parsed,
        "upserted": upserted,
        "skipped": len(skipped),
        "skipped_detail": skipped[:50],
    }


# ---------------- eligibility: quick add a single number ----------------
@app.post("/admin/eligibility/add")
async def admin_eligibility_add(request: Request):
    _require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    b = body or {}
    phone = _norm_phone(b.get("phone", ""))
    if not phone:
        raise HTTPException(status_code=400, detail="a valid 10-digit phone is required")

    def _c(v, n):
        s = str(v).strip() if v is not None else ""
        return s[:n] or None

    milk = b.get("milk_payment_amount")
    try:
        milk = float(milk) if milk not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="milk_payment_amount must be a number")

    rec = {
        "phone": phone,
        "farmer_code": _c(b.get("farmer_code"), 64),
        "mandali_name": _c(b.get("mandali_name"), 128),
        "sabhsad_name": _c(b.get("sabhsad_name"), 256),
        "ac_no": _c(b.get("ac_no"), 32),
        "milk_payment_amount": milk,
        "source_batch": "admin-manual-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    }
    sm = _get_sessionmaker()
    async with sm() as sess:
        await sess.execute(text(_ELIG_UPSERT_SQL), rec)
        await _audit(
            sess, actor=ADMIN_ACTOR, action="admin_add", lookup_kind="phone",
            phone=phone, result="ok", src_ip=_client_ip(request),
            detail={"farmer_code": rec["farmer_code"], "name": rec["sabhsad_name"]},
        )
        await sess.commit()
    return {"ok": True, "phone": phone, "farmer_code": rec["farmer_code"], "source_batch": rec["source_batch"]}


# ---------------- eligibility: search/list ----------------
@app.get("/admin/api/eligibility")
async def admin_eligibility_list(request: Request, q: str = Query("")):
    _require_admin(request)
    q = (q or "").strip()
    sm = _get_sessionmaker()
    like = f"%{q}%"
    async with sm() as sess:
        rows = (
            await sess.execute(
                text(
                    "SELECT id, phone, farmer_code, mandali_name, sabhsad_name, ac_no, "
                    "  milk_payment_amount, source_batch, is_active, created_at "
                    "FROM loan_eligibility_list "
                    "WHERE :q = '' OR phone LIKE :like OR sabhsad_name ILIKE :like "
                    "   OR farmer_code ILIKE :like OR mandali_name ILIKE :like "
                    "ORDER BY created_at DESC LIMIT 200"
                ),
                {"q": q, "like": like},
            )
        ).fetchall()
    out = []
    for r in rows:
        m = dict(r._mapping)
        out.append({
            "id": m["id"],
            "phone": m.get("phone"),
            "farmer_code": m.get("farmer_code"),
            "sabhsad_name": m.get("sabhsad_name"),
            "mandali_name": m.get("mandali_name"),
            "ac_no": m.get("ac_no"),
            "milk_payment_amount": float(m["milk_payment_amount"]) if m.get("milk_payment_amount") is not None else None,
            "source_batch": m.get("source_batch"),
            "is_active": m.get("is_active"),
            "created_at": _iso(m.get("created_at")),
        })
    return {"count": len(out), "rows": out}


# ---------------- eligibility: remove (hard delete by phone) ----------------
@app.post("/admin/eligibility/remove")
async def admin_eligibility_remove(request: Request):
    _require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    phone = _norm_phone((body or {}).get("phone", ""))
    if not phone:
        raise HTTPException(status_code=400, detail="a valid 10-digit phone is required")
    sm = _get_sessionmaker()
    async with sm() as sess:
        res = await sess.execute(
            text("DELETE FROM loan_eligibility_list WHERE phone = :phone RETURNING id"),
            {"phone": phone},
        )
        deleted = len(res.fetchall())
        await _audit(
            sess, actor=ADMIN_ACTOR, action="admin_remove", lookup_kind="phone",
            phone=phone, result=("ok" if deleted else "not_found"),
            src_ip=_client_ip(request), detail={"deleted": deleted},
        )
        await sess.commit()
    return {"ok": True, "phone": phone, "deleted": deleted}


# ---------------- codes: per-phone view ----------------
@app.get("/admin/api/codes")
async def admin_codes_for_phone(request: Request, phone: str = Query("")):
    _require_admin(request)
    norm = _norm_phone(phone)
    if not norm:
        raise HTTPException(status_code=400, detail="a valid 10-digit phone is required")
    sm = _get_sessionmaker()
    async with sm() as sess:
        rows = (
            await sess.execute(
                text(
                    "SELECT code, phone, farmer_name, farmer_code, mandali_name, "
                    "  loan_amount, status, issued_at, expires_at, redeemed_at, redeemed_by "
                    "FROM loan_codes WHERE phone = :phone "
                    "ORDER BY (status='active') DESC, issued_at DESC"
                ),
                {"phone": norm},
            )
        ).fetchall()
    out = []
    for r in rows:
        m = dict(r._mapping)
        out.append({
            "code": m["code"],
            "farmer_name": m.get("farmer_name"),
            "farmer_code": m.get("farmer_code"),
            "loan_amount": float(m["loan_amount"]) if m.get("loan_amount") is not None else None,
            "status": m.get("status"),
            "effective_status": _effective_status(m["status"], m.get("expires_at")),
            "issued_at": _iso(m.get("issued_at")),
            "expires_at": _iso(m.get("expires_at")),
            "redeemed_at": _iso(m.get("redeemed_at")),
            "redeemed_by": m.get("redeemed_by"),
        })
    return {"phone": norm, "count": len(out), "codes": out}


# ---------------- codes: clear/cancel active (lets farmer regenerate) --------
@app.post("/admin/codes/clear")
async def admin_codes_clear(request: Request):
    _require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    phone = _norm_phone((body or {}).get("phone", ""))
    if not phone:
        raise HTTPException(status_code=400, detail="a valid 10-digit phone is required")
    sm = _get_sessionmaker()
    async with sm() as sess:
        # Cancel (do NOT delete) — preserves the record while resetting the
        # already-availed guard, so the farmer can regenerate via chat/voice.
        res = await sess.execute(
            text(
                "UPDATE loan_codes SET status='cancelled', updated_at=now() "
                "WHERE phone = :phone AND status='active' RETURNING code"
            ),
            {"phone": phone},
        )
        cleared = len(res.fetchall())
        await _audit(
            sess, actor=ADMIN_ACTOR, action="admin_clear", lookup_kind="phone",
            phone=phone, result=("ok" if cleared else "not_found"),
            src_ip=_client_ip(request), detail={"cleared": cleared},
        )
        await sess.commit()
    return {"ok": True, "phone": phone, "cleared": cleared}


# =======================================================================
# UI (server-rendered, self-contained: inline CSS + inline Amul logo)
# =======================================================================
AMUL_LOGO = """<svg width="36" height="36" viewBox="0 0 255 255" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Amul">
  <g clip-path="url(#amulClip)">
    <path d="M127.48 254.962C197.886 254.962 254.961 197.886 254.961 127.481C254.961 57.0751 197.886 0 127.48 0C57.0747 0 -0.0004 57.0751 -0.0004 127.481C-0.0004 197.886 57.0747 254.962 127.48 254.962Z" fill="url(#amulGrad)"/>
    <path fill-rule="evenodd" clip-rule="evenodd" d="M113.533 160.215C106.771 159.677 98.8564 158.255 90.327 155.643L83.2191 173.585L99.1638 183.959C101.277 185.573 104.389 184.42 108.654 180.386L112.304 183.498C104.85 191.643 97.3964 199.904 89.9812 208.049C80.107 199.174 69.0802 190.337 57.477 190.375C45.0286 190.452 33.0797 203.131 45.6818 224.993C45.7586 225.377 43.7992 227.951 42.6465 226.76C32.8492 219.153 29.1223 197.06 36.7297 184.535C41.6092 176.621 51.4065 168.821 66.8902 170.55L73.3449 153.222C67.8891 151.109 61.2039 153.299 53.4428 159.984C56.017 146.114 64.6617 136.855 81.9512 134.319L99.6632 91.5565C90.4038 87.5223 81.567 92.4018 77.2638 100.432C74.5359 100.201 72.6917 99.3176 72.1923 97.3965L90.4038 63.5092C99.0101 60.5508 106.656 60.4739 113.456 62.6639C141.043 71.4623 155.527 118.451 176.352 170.243C178.08 174.738 180.616 175.199 186.379 169.244L190.606 172.74L153.376 210.124L136.278 158.64C136.278 159.37 127.326 161.367 113.495 160.215H113.533ZM113.533 140.082C118.989 140.735 124.791 140.812 129.478 139.66C127.326 130.554 118.221 109.461 113.533 103.429C112.995 102.737 112.534 102.276 112.15 101.969L97.6269 137.239C97.6269 137.239 105.004 139.16 113.572 140.082H113.533Z" fill="#fff"/>
    <path fill-rule="evenodd" clip-rule="evenodd" d="M229.527 87.2919V91.4797C205.975 94.9376 196.985 103.928 193.527 127.48H189.339C185.881 103.967 176.891 94.9376 153.377 91.4797V87.2919C176.891 83.834 185.881 74.8435 189.339 51.3298H193.527C196.985 74.8435 206.014 83.834 229.527 87.2919Z" fill="#fff"/>
    <path fill-rule="evenodd" clip-rule="evenodd" d="M177.198 46.95V49.2168C164.481 51.0994 159.64 55.9405 157.757 68.6578H155.49C153.607 55.9405 148.766 51.0994 136.088 49.2168V46.95C148.805 45.0673 153.646 40.2263 155.49 27.5474H157.757C159.64 40.2647 164.481 45.1057 177.198 46.95Z" fill="#fff"/>
  </g>
  <defs>
    <linearGradient id="amulGrad" x1="212.813" y1="42.1478" x2="29.968" y2="224.993" gradientUnits="userSpaceOnUse">
      <stop stop-color="#218FFF"/><stop offset="1" stop-color="#FF1150"/>
    </linearGradient>
    <clipPath id="amulClip"><rect width="255" height="255" fill="#fff"/></clipPath>
  </defs>
</svg>"""

# Shared design tokens + component CSS (from CHAT_STYLE.md — inlined, light-only).
STYLE = """
:root{
  --primary:#F65151; --primary-hover:#E23B3B; --primary-foreground:#fff;
  --primary-tint:#FFE2E2; --primary-tint-hover:#FFCCCC;
  --secondary:#FEF89E; --secondary-foreground:#455314; --accent:#00BD52;
  --brand-grad:linear-gradient(90deg,#218FFF 0%,#FF1150 100%);
  --brand-red:#EC1C24;
  --page-bg:linear-gradient(180deg,#FFF2F2 0%,#FFFFFF 100%);
  --background:#FFF2F2; --foreground:#3D3D3D; --card:#fff; --card-foreground:#111827;
  --surface-header:#fff; --muted:#F3F4F6; --muted-foreground:#6B7280;
  --border:#E5E7EB; --border-subtle:#F3F4F6; --border-header:#E6E8EC; --input-border:#E3E3E3;
  --gray-50:#f9fafb; --gray-500:#6b7280; --gray-600:#4b5563; --gray-900:#111827;
  --success:#00A651; --danger:#C02626; --warning:#FEF89E; --ring:#72FFB3;
  --font-family:"Noto Sans",system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --radius-md:.5rem; --radius-xl:.875rem; --radius-2xl:1rem; --radius-full:9999px;
  --shadow-sm:0 1px 2px 0 rgb(0 0 0 / .05);
  --shadow-md:0 4px 6px -1px rgb(0 0 0 / .10),0 2px 4px -2px rgb(0 0 0 / .10);
  --container:48rem;
}
*{box-sizing:border-box}
html,body{font-family:var(--font-family);font-size:.9rem;color:var(--foreground);margin:0}
body{background:var(--page-bg);background-attachment:fixed;min-height:100svh}
.topbar{position:sticky;top:0;z-index:40;display:flex;align-items:center;justify-content:space-between;
  padding:.85rem 1rem;background:var(--surface-header);border-bottom:1px solid var(--border-header);
  -webkit-backdrop-filter:saturate(110%) blur(6px);backdrop-filter:saturate(110%) blur(6px)}
.brand{display:flex;align-items:center;gap:.6rem}
.brand-title{font-size:1.1rem;font-weight:600;color:#000;line-height:1.1}
.brand-sub{font-size:.72rem;color:var(--muted-foreground);font-weight:400}
.logout{color:var(--muted-foreground);font-size:.8rem;text-decoration:none;background:none;border:0;cursor:pointer;font-family:inherit}
.logout:hover{color:var(--primary)}
.switch-btn{display:inline-flex;align-items:center;gap:.3rem;font-size:.78rem;font-weight:600;color:var(--primary);background:var(--primary-tint);border:1px solid var(--primary);border-radius:8px;padding:.4rem .7rem;text-decoration:none;white-space:nowrap;line-height:1}
.switch-btn:hover{background:var(--primary);color:var(--primary-foreground)}
.switch-corner{position:fixed;top:1rem;right:1rem;z-index:50}
.topbar-actions{display:flex;align-items:center;gap:.75rem}
.container{max-width:var(--container);margin:0 auto;padding:1.25rem 1rem 3rem}
.card{background:var(--card);color:var(--card-foreground);border:1px solid var(--border-subtle);
  border-radius:var(--radius-2xl);padding:1.5rem;box-shadow:var(--shadow-sm)}
.search-card{margin-bottom:1rem}
.search-card h2{margin:0 0 .25rem;font-size:1rem;font-weight:600}
.hint{color:var(--muted-foreground);font-size:.8rem;margin:0 0 1rem}
.search-row{display:flex;gap:.6rem;flex-wrap:wrap}
.input{flex:1 1 220px;height:2.5rem;padding:.25rem .85rem;border:1px solid var(--input-border);
  border-radius:var(--radius-md);background:#fff;font-size:1rem;color:var(--foreground);
  box-shadow:var(--shadow-sm);outline:none;font-family:inherit;transition:box-shadow .15s,border-color .15s}
.input::placeholder{color:var(--muted-foreground)}
.input:focus-visible{border-color:var(--ring);box-shadow:0 0 0 3px color-mix(in srgb,var(--ring) 50%,transparent)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;height:2.5rem;padding:0 1.25rem;
  border-radius:var(--radius-md);font-weight:500;font-size:.9rem;white-space:nowrap;cursor:pointer;
  border:1px solid transparent;font-family:inherit;transition:all .15s ease}
.btn-primary{background:var(--primary);color:var(--primary-foreground)}
.btn-primary:hover{background:var(--primary-hover)}
.btn-outline{background:var(--card);border-color:var(--border);color:var(--foreground);box-shadow:var(--shadow-sm)}
.btn-outline:hover{background:var(--primary-tint);color:var(--primary)}
.btn:disabled{opacity:.5;pointer-events:none}
.results{margin-top:1.25rem;display:flex;flex-direction:column;gap:1rem}
.loan-card{background:var(--card);border:1px solid var(--border-subtle);border-radius:var(--radius-2xl);
  padding:1.25rem 1.4rem;box-shadow:var(--shadow-sm)}
.lc-top{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem}
.lc-name{font-size:1.05rem;font-weight:600;color:var(--gray-900);margin:0}
.lc-phone{color:var(--muted-foreground);font-size:.85rem;margin:.15rem 0 0;font-variant-numeric:tabular-nums}
.lc-amount{font-size:1.4rem;font-weight:700;color:var(--gray-900);white-space:nowrap}
.lc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem 1.25rem;margin:1rem 0}
.lc-field .k{font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted-foreground)}
.lc-field .v{font-size:.9rem;color:var(--gray-900);margin-top:.1rem;font-weight:500}
.lc-code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.12em;font-weight:600}
.badge{display:inline-flex;align-items:center;gap:.3rem;padding:.2rem .6rem;border-radius:var(--radius-full);
  font-size:.75rem;font-weight:500;border:1px solid transparent}
.badge-active{background:color-mix(in srgb,var(--success) 15%,#fff);color:var(--success)}
.badge-redeemed{background:var(--primary-tint);color:var(--primary-hover)}
.badge-expired{background:var(--muted);color:var(--gray-600)}
.badge-cancelled{background:color-mix(in srgb,var(--danger) 12%,#fff);color:var(--danger)}
.reason{margin-top:.5rem;padding:.7rem .9rem;border-radius:var(--radius-md);font-size:.85rem}
.reason-redeemed{background:var(--primary-tint);color:var(--primary-hover)}
.reason-expired{background:var(--muted);color:var(--gray-600)}
.reason-cancelled{background:color-mix(in srgb,var(--danger) 12%,#fff);color:var(--danger)}
.lc-actions{margin-top:.75rem;display:flex;gap:.6rem;align-items:center}
.msg{margin-top:1rem;padding:.8rem 1rem;border-radius:var(--radius-md);font-size:.88rem;display:none}
.msg-info{background:var(--muted);color:var(--gray-600)}
.msg-error{background:color-mix(in srgb,var(--danger) 12%,#fff);color:var(--danger)}
.msg-ok{background:color-mix(in srgb,var(--success) 15%,#fff);color:var(--success)}
.export-card{margin-top:1.5rem}
.export-card h3{margin:0 0 .5rem;font-size:.95rem;font-weight:600}
.export-row{display:flex;gap:.6rem;align-items:flex-end;flex-wrap:wrap}
.export-row label{display:flex;flex-direction:column;gap:.25rem;font-size:.78rem;color:var(--muted-foreground)}
.export-row input[type=date]{height:2.4rem;padding:.25rem .6rem;border:1px solid var(--input-border);
  border-radius:var(--radius-md);font-size:.9rem;font-family:inherit;color:var(--foreground)}
.empty{color:var(--muted-foreground);text-align:center;padding:1.5rem;font-size:.9rem}
"""

FONT_LINK = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
             '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
             '<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">')


def render_login(err: str) -> str:
    err_html = f'<div class="msg msg-error" style="display:block">{err}</div>' if err else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Amul Loan Portal — Sign in</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
{FONT_LINK}<style>{STYLE}
.login-wrap{{min-height:100svh;display:flex;align-items:center;justify-content:center;padding:1rem}}
.login-card{{width:340px;max-width:100%}}
.login-head{{display:flex;flex-direction:column;align-items:center;gap:.6rem;margin-bottom:1.25rem;text-align:center}}
.login-card h1{{font-size:1.05rem;margin:0;font-weight:600}}
.login-card .sub{{color:var(--muted-foreground);font-size:.8rem;margin:0}}
.login-card .input{{margin-bottom:.85rem;width:100%}}
.login-card .btn{{width:100%}}
</style></head><body>
<a class="switch-btn switch-corner" href="/admin/login">Admin &rarr;</a>
<div class="login-wrap"><form class="card login-card" method="post" action="/login">
  <div class="login-head">{AMUL_LOGO}
    <div><h1>Loan Verification Portal</h1><p class="sub">Bank staff sign-in</p></div>
  </div>
  <input class="input" type="password" name="password" placeholder="Portal password" autofocus autocomplete="current-password">
  <button class="btn btn-primary" type="submit">Sign in</button>
  {err_html}
</form></div></body></html>"""


def render_portal() -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Amul Loan Verification Portal</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
{FONT_LINK}<style>{STYLE}</style></head><body>
<header class="topbar">
  <div class="brand">{AMUL_LOGO}
    <div><div class="brand-title">Loan Verification Portal</div>
      <div class="brand-sub">Verify &amp; issue micro-loan codes</div></div>
  </div>
  <div class="topbar-actions">
    <a class="switch-btn" href="/admin/login">Admin</a>
    <form method="post" action="/logout" style="margin:0"><button class="logout" type="submit">Sign out</button></form>
  </div>
</header>
<main class="container">
  <section class="card search-card">
    <h2>Find a loan code</h2>
    <p class="hint">Enter the farmer's <strong>10-digit mobile number</strong> (preferred) or a <strong>6-digit code</strong>.</p>
    <form id="searchForm" class="search-row" onsubmit="return doSearch(event)">
      <input id="q" class="input" inputmode="numeric" autocomplete="off"
        placeholder="Mobile number or 6-digit code" autofocus>
      <button class="btn btn-primary" type="submit">Verify</button>
    </form>
    <div id="msg" class="msg"></div>
  </section>

  <div id="results" class="results"></div>

  <section class="card export-card">
    <h3>Reconciliation export</h3>
    <p class="hint">Download issued codes for a date range (CSV).</p>
    <form class="export-row" onsubmit="return doExport(event)">
      <label>From <input type="date" id="expFrom"></label>
      <label>To <input type="date" id="expTo"></label>
      <button class="btn btn-outline" type="submit">Download CSV</button>
    </form>
  </section>
</main>
<script>{PORTAL_JS}</script>
</body></html>"""


PORTAL_JS = r"""
var msgEl = document.getElementById('msg');
function showMsg(text, kind){ msgEl.textContent=text; msgEl.className='msg msg-'+kind; msgEl.style.display=text?'block':'none'; }
function esc(s){ if(s===null||s===undefined) return ''; return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }
function fmtDate(iso){ if(!iso) return '—'; try{ var d=new Date(iso); if(isNaN(d)) return iso; return d.toLocaleString(); }catch(e){ return iso; } }
function money(n){ if(n===null||n===undefined) return '—'; return '₹'+Number(n).toLocaleString('en-IN'); }

function badge(eff){
  var map={active:'Active',redeemed:'Issued',expired:'Expired',cancelled:'Cancelled'};
  var cls={active:'badge-active',redeemed:'badge-redeemed',expired:'badge-expired',cancelled:'badge-cancelled'}[eff]||'badge-expired';
  return '<span class="badge '+cls+'">'+(map[eff]||eff)+'</span>';
}

function field(k,v){ return '<div class="lc-field"><div class="k">'+esc(k)+'</div><div class="v">'+v+'</div></div>'; }

function renderCard(c){
  var eff=c.effective_status;
  var reason='';
  if(eff==='redeemed'){ reason='<div class="reason reason-redeemed">Already issued on '+esc(fmtDate(c.redeemed_at))+(c.redeemed_by?(' by '+esc(c.redeemed_by)):'')+'.</div>'; }
  else if(eff==='expired'){ reason='<div class="reason reason-expired">This code has expired'+(c.expires_at?(' ('+esc(fmtDate(c.expires_at))+')'):'')+'.</div>'; }
  else if(eff==='cancelled'){ reason='<div class="reason reason-cancelled">This code was cancelled.</div>'; }

  var action='';
  if(eff==='active'){
    action='<div class="lc-actions"><button class="btn btn-primary" onclick="redeem(this,\''+esc(c.code)+'\',\''+esc(c.redeem_nonce||'')+'\')">Mark as issued</button></div>';
  }
  return '<div class="loan-card" data-code="'+esc(c.code)+'">'
    + '<div class="lc-top"><div><h3 class="lc-name">'+(esc(c.farmer_name)||'Unknown farmer')+'</h3>'
    + '<p class="lc-phone">'+esc(c.phone_masked)+'</p></div>'
    + '<div style="text-align:right"><div class="lc-amount">'+money(c.loan_amount)+'</div>'
    + '<div style="margin-top:.35rem">'+badge(eff)+'</div></div></div>'
    + '<div class="lc-grid">'
    + field('Code','<span class="lc-code">'+esc(c.code)+'</span>')
    + field('Farmer code', esc(c.farmer_code)||'—')
    + field('Mandali', esc(c.mandali_name)||'—')
    + field('Union', esc(c.union_code)||'—')
    + field('Issued', esc(fmtDate(c.issued_at)))
    + field('Expires', esc(fmtDate(c.expires_at)))
    + '</div>' + reason + action + '</div>';
}

function renderCards(codes){
  var box=document.getElementById('results');
  if(!codes.length){ box.innerHTML='<div class="card empty">No loan code found.</div>'; return; }
  box.innerHTML=codes.map(renderCard).join('');
}

async function doSearch(ev){
  ev.preventDefault();
  showMsg('','info');
  var raw=document.getElementById('q').value.trim().replace(/\s+/g,'');
  document.getElementById('results').innerHTML='';
  if(/^\d{10}$/.test(raw)){ return runVerify('phone='+encodeURIComponent(raw)); }
  if(/^\d{6}$/.test(raw)){ return runVerify('code='+encodeURIComponent(raw)); }
  showMsg('Enter a 10-digit mobile number or a 6-digit code.','error');
  return false;
}

async function runVerify(qs){
  try{
    var r=await fetch('/api/verify?'+qs,{headers:{'Accept':'application/json'}});
    var data=await r.json();
    if(r.status===429){ showMsg(data.message||'Rate limited.','error'); return false; }
    if(!r.ok){ showMsg(data.detail||'Lookup failed.','error'); return false; }
    renderCards(data.codes||[]);
  }catch(e){ showMsg('Network error.','error'); }
  return false;
}

async function redeem(btn, code, nonce){
  btn.disabled=true; btn.textContent='Issuing…';
  try{
    var r=await fetch('/api/redeem',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:code,nonce:nonce})});
    var data=await r.json();
    if(!r.ok){ showMsg(data.detail||'Issue failed.','error'); btn.disabled=false; btn.textContent='Mark as issued'; return; }
    var card=document.querySelector('.loan-card[data-code="'+code.replace(/"/g,'')+'"]');
    if(data.code && card){ card.outerHTML=renderCard(data.code); }
    showMsg(data.message||(data.redeemed?'Issued.':'Not issued.'), data.redeemed?'ok':'info');
  }catch(e){ showMsg('Network error.','error'); btn.disabled=false; btn.textContent='Mark as issued'; }
}

function doExport(ev){
  ev.preventDefault();
  var f=document.getElementById('expFrom').value, t=document.getElementById('expTo').value;
  if(!f||!t){ showMsg('Pick both dates for the export.','error'); return false; }
  window.location='/api/export.csv?from='+encodeURIComponent(f)+'&to='+encodeURIComponent(t);
  return false;
}
"""


# =======================================================================
# Admin UI (server-rendered; reuses the portal STYLE/logo + a little extra CSS)
# =======================================================================
ADMIN_STYLE = """
.qa-grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}
@media(max-width:520px){.qa-grid{grid-template-columns:1fr}}
.section-title{font-size:1rem;font-weight:600;margin:0 0 .25rem}
.admin-grid{display:flex;flex-direction:column;gap:1rem}
.file-row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-top:.5rem}
input[type=file]{font-family:inherit;font-size:.85rem}
.tag-input{flex:0 1 220px;height:2.5rem;padding:.25rem .85rem;border:1px solid var(--input-border);
  border-radius:var(--radius-md);background:#fff;font-size:.9rem;font-family:inherit;color:var(--foreground)}
.table-wrap{overflow-x:auto;margin-top:1rem}
table.tbl{width:100%;border-collapse:collapse;font-size:.82rem}
table.tbl th{text-align:left;font-size:.68rem;text-transform:uppercase;letter-spacing:.04em;
  color:var(--muted-foreground);font-weight:600;padding:.5rem .6rem;border-bottom:1px solid var(--border)}
table.tbl td{padding:.5rem .6rem;border-bottom:1px solid var(--border-subtle);color:var(--gray-900);vertical-align:top}
table.tbl tr:last-child td{border-bottom:0}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.btn-danger{background:color-mix(in srgb,var(--danger) 12%,#fff);color:var(--danger);border-color:transparent}
.btn-danger:hover{background:color-mix(in srgb,var(--danger) 22%,#fff)}
.btn-sm{height:2rem;padding:0 .75rem;font-size:.8rem}
.pill-inactive{color:var(--muted-foreground)}
.summary{margin-top:.75rem;padding:.7rem .9rem;border-radius:var(--radius-md);font-size:.83rem;
  background:var(--muted);color:var(--gray-600);white-space:pre-wrap}
"""


def render_admin_login(err: str) -> str:
    err_html = f'<div class="msg msg-error" style="display:block">{err}</div>' if err else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Amul Loan Portal — Admin sign in</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
{FONT_LINK}<style>{STYLE}{ADMIN_STYLE}
.login-wrap{{min-height:100svh;display:flex;align-items:center;justify-content:center;padding:1rem}}
.login-card{{width:340px;max-width:100%}}
.login-head{{display:flex;flex-direction:column;align-items:center;gap:.6rem;margin-bottom:1.25rem;text-align:center}}
.login-card h1{{font-size:1.05rem;margin:0;font-weight:600}}
.login-card .sub{{color:var(--muted-foreground);font-size:.8rem;margin:0}}
.login-card .input{{margin-bottom:.85rem;width:100%}}
.login-card .btn{{width:100%}}
</style></head><body>
<a class="switch-btn switch-corner" href="/login">Bank portal &rarr;</a>
<div class="login-wrap"><form class="card login-card" method="post" action="/admin/login">
  <div class="login-head">{AMUL_LOGO}
    <div><h1>Loan Portal — Admin</h1><p class="sub">Eligibility &amp; code management</p></div>
  </div>
  <input class="input" type="password" name="password" placeholder="Admin password" autofocus autocomplete="current-password">
  <button class="btn btn-primary" type="submit">Sign in</button>
  {err_html}
</form></div></body></html>"""


def render_admin() -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Amul Loan Portal — Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
{FONT_LINK}<style>{STYLE}{ADMIN_STYLE}</style></head><body>
<header class="topbar">
  <div class="brand">{AMUL_LOGO}
    <div><div class="brand-title">Loan Portal — Admin</div>
      <div class="brand-sub">Manage eligibility list &amp; loan codes</div></div>
  </div>
  <div class="topbar-actions">
    <a class="switch-btn" href="/login">Bank portal</a>
    <form method="post" action="/admin/logout" style="margin:0"><button class="logout" type="submit">Sign out</button></form>
  </div>
</header>
<main class="container">
  <div id="msg" class="msg"></div>
  <div class="admin-grid">

    <section class="card">
      <h2 class="section-title">Add eligible people (upload)</h2>
      <p class="hint">Upload a SABHSAD <strong>.csv</strong> or <strong>.xlsx</strong>
        (columns: CODE, AC NO, SABHSAD NAME, MILK PAYMENT AMOUNT, MANDALI NAME, MOBILE NO).
        Rows upsert on (phone, farmer&nbsp;code); short/blank phones are skipped.</p>
      <form id="uploadForm" onsubmit="return doUpload(event)">
        <div class="file-row">
          <input type="file" id="upFile" accept=".csv,.xlsx" required>
          <input class="tag-input" id="upTag" placeholder="batch tag (optional)" autocomplete="off">
          <button class="btn btn-primary" type="submit">Upload</button>
        </div>
      </form>
      <div id="upSummary" class="summary" style="display:none"></div>
    </section>

    <section class="card">
      <h2 class="section-title">Quick add a number</h2>
      <p class="hint">Add a single eligible number without a file — handy for test numbers.
        Phone is required; the rest are optional.</p>
      <form id="quickAddForm" onsubmit="return doQuickAdd(event)">
        <div class="qa-grid">
          <input class="input" id="qaPhone" inputmode="numeric" autocomplete="off" placeholder="10-digit mobile *" required>
          <input class="input" id="qaName" autocomplete="off" placeholder="Name (optional)">
          <input class="input" id="qaCode" autocomplete="off" placeholder="Farmer code (optional)">
          <input class="input" id="qaMandali" autocomplete="off" placeholder="Mandali (optional)">
        </div>
        <div style="margin-top:.75rem"><button class="btn btn-primary" type="submit">Add number</button></div>
      </form>
      <div id="qaSummary" class="summary" style="display:none"></div>
    </section>

    <section class="card">
      <h2 class="section-title">Eligibility list</h2>
      <p class="hint">Search by phone or name, then remove a person (hard delete of all their rows).</p>
      <form class="search-row" onsubmit="return doEligSearch(event)">
        <input id="eligQ" class="input" autocomplete="off" placeholder="Phone or name (blank = latest 200)">
        <button class="btn btn-outline" type="submit">Search</button>
      </form>
      <div class="table-wrap"><table class="tbl" id="eligTbl">
        <thead><tr><th>Name</th><th>Phone</th><th>Farmer code</th><th>Mandali</th><th>Batch</th><th>Active</th><th></th></tr></thead>
        <tbody id="eligBody"><tr><td colspan="7" class="empty">Search to list eligible people.</td></tr></tbody>
      </table></div>
    </section>

    <section class="card">
      <h2 class="section-title">Clear codes / let a farmer regenerate</h2>
      <p class="hint">Enter a phone to see their codes. <strong>Cancel active</strong> flips active codes to
        <em>cancelled</em> (the record is preserved). This resets the already-availed guard so the farmer can
        regenerate a fresh code through the normal chat/voice flow.</p>
      <form class="search-row" onsubmit="return doCodesLookup(event)">
        <input id="codesPhone" class="input" inputmode="numeric" autocomplete="off" placeholder="10-digit phone">
        <button class="btn btn-outline" type="submit">Look up codes</button>
      </form>
      <div class="table-wrap"><table class="tbl" id="codesTbl">
        <thead><tr><th>Code</th><th>Name</th><th>Amount</th><th>Status</th><th>Issued</th><th>Expires</th></tr></thead>
        <tbody id="codesBody"><tr><td colspan="6" class="empty">Look up a phone to see codes.</td></tr></tbody>
      </table></div>
      <div class="lc-actions" id="codesActions" style="display:none;margin-top:1rem">
        <button class="btn btn-danger" onclick="doCodesClear()">Cancel active codes</button>
        <span class="hint" style="margin:0">Farmer then regenerates via chat/voice.</span>
      </div>
    </section>

  </div>
</main>
<script>{ADMIN_JS}</script>
</body></html>"""


ADMIN_JS = r"""
var msgEl = document.getElementById('msg');
var currentCodesPhone = null;
function showMsg(text, kind){ msgEl.textContent=text; msgEl.className='msg msg-'+kind; msgEl.style.display=text?'block':'none'; }
function esc(s){ if(s===null||s===undefined) return ''; return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }
function fmtDate(iso){ if(!iso) return '—'; try{ var d=new Date(iso); if(isNaN(d)) return iso; return d.toLocaleDateString(); }catch(e){ return iso; } }
function money(n){ if(n===null||n===undefined) return '—'; return '₹'+Number(n).toLocaleString('en-IN'); }
function badge(eff){
  var map={active:'Active',redeemed:'Issued',expired:'Expired',cancelled:'Cancelled'};
  var cls={active:'badge-active',redeemed:'badge-redeemed',expired:'badge-expired',cancelled:'badge-cancelled'}[eff]||'badge-expired';
  return '<span class="badge '+cls+'">'+(map[eff]||eff)+'</span>';
}

async function doUpload(ev){
  ev.preventDefault();
  showMsg('','info');
  var f=document.getElementById('upFile').files[0];
  var sum=document.getElementById('upSummary');
  if(!f){ showMsg('Choose a .csv or .xlsx file.','error'); return false; }
  var fd=new FormData();
  fd.append('file', f);
  fd.append('tag', document.getElementById('upTag').value.trim());
  try{
    var r=await fetch('/admin/eligibility/upload',{method:'POST',body:fd});
    var data=await r.json();
    if(!r.ok){ showMsg(data.detail||'Upload failed.','error'); return false; }
    var lines='Batch: '+data.batch+'\nParsed: '+data.parsed+'   Upserted: '+data.upserted+'   Skipped: '+data.skipped;
    if(data.skipped_detail && data.skipped_detail.length){
      lines+='\n\nSkipped rows:';
      data.skipped_detail.forEach(function(s){ lines+='\n  row '+s.row+': '+s.reason; });
    }
    sum.textContent=lines; sum.style.display='block';
    showMsg('Uploaded — '+data.upserted+' upserted, '+data.skipped+' skipped.','ok');
    doEligSearch(null);
  }catch(e){ showMsg('Network error during upload.','error'); }
  return false;
}

async function doQuickAdd(ev){
  ev.preventDefault();
  showMsg('','info');
  var body={ phone:document.getElementById('qaPhone').value.trim(),
    sabhsad_name:document.getElementById('qaName').value.trim(),
    farmer_code:document.getElementById('qaCode').value.trim(),
    mandali_name:document.getElementById('qaMandali').value.trim() };
  try{
    var r=await fetch('/admin/eligibility/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    var data=await r.json();
    if(!r.ok){ showMsg(data.detail||'Add failed.','error'); return false; }
    showMsg('Added '+data.phone+(data.farmer_code?(' ('+esc(data.farmer_code)+')'):'')+' to the eligibility list.','ok');
    document.getElementById('quickAddForm').reset();
    doEligSearch(null);
  }catch(e){ showMsg('Network error.','error'); }
  return false;
}

async function doEligSearch(ev){
  if(ev) ev.preventDefault();
  var q=document.getElementById('eligQ').value.trim();
  var body=document.getElementById('eligBody');
  try{
    var r=await fetch('/admin/api/eligibility?q='+encodeURIComponent(q),{headers:{'Accept':'application/json'}});
    var data=await r.json();
    if(!r.ok){ showMsg(data.detail||'Search failed.','error'); return false; }
    if(!data.rows.length){ body.innerHTML='<tr><td colspan="7" class="empty">No matching people.</td></tr>'; return false; }
    body.innerHTML=data.rows.map(function(x){
      return '<tr>'
        + '<td>'+(esc(x.sabhsad_name)||'—')+'</td>'
        + '<td class="mono">'+esc(x.phone)+'</td>'
        + '<td>'+(esc(x.farmer_code)||'—')+'</td>'
        + '<td>'+(esc(x.mandali_name)||'—')+'</td>'
        + '<td>'+(esc(x.source_batch)||'—')+'</td>'
        + '<td>'+(x.is_active?'yes':'<span class="pill-inactive">no</span>')+'</td>'
        + '<td><button class="btn btn-danger btn-sm" onclick="removeElig(\''+esc(x.phone)+'\',\''+esc(x.sabhsad_name||'')+'\')">Remove</button></td>'
        + '</tr>';
    }).join('');
  }catch(e){ showMsg('Network error.','error'); }
  return false;
}

async function removeElig(phone, name){
  if(!confirm('Hard-delete ALL eligibility rows for '+(name||phone)+' ('+phone+')?\nThis cannot be undone.')) return;
  try{
    var r=await fetch('/admin/eligibility/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone:phone})});
    var data=await r.json();
    if(!r.ok){ showMsg(data.detail||'Remove failed.','error'); return; }
    showMsg('Removed '+data.deleted+' row(s) for '+phone+'.', data.deleted?'ok':'info');
    doEligSearch(null);
  }catch(e){ showMsg('Network error.','error'); }
}

async function doCodesLookup(ev){
  if(ev) ev.preventDefault();
  showMsg('','info');
  var raw=document.getElementById('codesPhone').value.trim().replace(/\D/g,'');
  var body=document.getElementById('codesBody');
  var actions=document.getElementById('codesActions');
  actions.style.display='none'; currentCodesPhone=null;
  if(raw.length<10){ showMsg('Enter a 10-digit phone.','error'); return false; }
  try{
    var r=await fetch('/admin/api/codes?phone='+encodeURIComponent(raw),{headers:{'Accept':'application/json'}});
    var data=await r.json();
    if(!r.ok){ showMsg(data.detail||'Lookup failed.','error'); return false; }
    currentCodesPhone=data.phone;
    if(!data.codes.length){ body.innerHTML='<tr><td colspan="6" class="empty">No codes for this phone.</td></tr>'; return false; }
    var anyActive=false;
    body.innerHTML=data.codes.map(function(c){
      if(c.effective_status==='active') anyActive=true;
      return '<tr>'
        + '<td class="mono">'+esc(c.code)+'</td>'
        + '<td>'+(esc(c.farmer_name)||'—')+'</td>'
        + '<td>'+money(c.loan_amount)+'</td>'
        + '<td>'+badge(c.effective_status)+'</td>'
        + '<td>'+esc(fmtDate(c.issued_at))+'</td>'
        + '<td>'+esc(fmtDate(c.expires_at))+'</td>'
        + '</tr>';
    }).join('');
    actions.style.display=anyActive?'flex':'none';
    if(!anyActive){ showMsg('No active codes to cancel for this phone.','info'); }
  }catch(e){ showMsg('Network error.','error'); }
  return false;
}

async function doCodesClear(){
  if(!currentCodesPhone) return;
  if(!confirm('Cancel ALL active codes for '+currentCodesPhone+'?\nThe farmer can then regenerate a new code via chat/voice.')) return;
  try{
    var r=await fetch('/admin/codes/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone:currentCodesPhone})});
    var data=await r.json();
    if(!r.ok){ showMsg(data.detail||'Clear failed.','error'); return; }
    showMsg('Cancelled '+data.cleared+' active code(s) for '+data.phone+'. Farmer can regenerate now.', data.cleared?'ok':'info');
    document.getElementById('codesPhone').value=data.phone;
    doCodesLookup(null);
  }catch(e){ showMsg('Network error.','error'); }
}
"""
