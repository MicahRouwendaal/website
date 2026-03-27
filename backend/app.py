from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import create_engine, text


APP_ROOT = Path(__file__).resolve().parent
SITE_FILE = APP_ROOT / os.getenv("SITE_FILE", "generated-site.html")
raw_db = os.getenv("SQLALCHEMY_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()
if raw_db.startswith("postgres://"):
    raw_db = "postgresql://" + raw_db[len("postgres://"):]
if raw_db.startswith("postgresql://"):
    raw_db = "postgresql+psycopg://" + raw_db[len("postgresql://"):]
DATABASE_URL = raw_db
print("DB scheme:", DATABASE_URL.split("://", 1)[0])  # temporary debug
TOKEN_SECRET = os.getenv("TOKEN_SECRET", "").strip() or "change-this-token-secret"

TOKEN_TTL_HOURS = max(1, int(os.getenv("TOKEN_TTL_HOURS", "12") or "12"))
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").strip().lower() not in {"0", "false", "no", "off"}
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
DB_POOL_SIZE = max(1, int(os.getenv("DB_POOL_SIZE", "10") or "10"))
DB_MAX_OVERFLOW = max(0, int(os.getenv("DB_MAX_OVERFLOW", "20") or "20"))
DB_POOL_RECYCLE = max(60, int(os.getenv("DB_POOL_RECYCLE", "1800") or "1800"))
DB_POOL_TIMEOUT = max(1, int(os.getenv("DB_POOL_TIMEOUT", "30") or "30"))
LOGIN_RATE_LIMIT = max(1, int(os.getenv("LOGIN_RATE_LIMIT", "8") or "8"))
LOGIN_RATE_WINDOW_SEC = max(30, int(os.getenv("LOGIN_RATE_WINDOW_SEC", "900") or "900"))
BOOKING_RATE_LIMIT = max(1, int(os.getenv("BOOKING_RATE_LIMIT", "40") or "40"))
BOOKING_RATE_WINDOW_SEC = max(30, int(os.getenv("BOOKING_RATE_WINDOW_SEC", "600") or "600"))
MIN_STAFF_PASSWORD_LENGTH = max(8, int(os.getenv("MIN_STAFF_PASSWORD_LENGTH", "10") or "10"))
_FAILED_LOGIN: dict[str, list[float]] = {}
_BOOKING_REQUESTS: dict[str, list[float]] = {}

if not DATABASE_URL:
  raise RuntimeError("DATABASE_URL is required.")
if AUTH_REQUIRED and TOKEN_SECRET in {"", "change-this-token-secret"}:
  raise RuntimeError("Set a strong TOKEN_SECRET in .env before production use.")
if AUTH_REQUIRED and "replace_" in ADMIN_PASSWORD_HASH:
  raise RuntimeError("Replace ADMIN_PASSWORD_HASH placeholder with a generated hash.")
if AUTH_REQUIRED and not ADMIN_PASSWORD_HASH and ADMIN_PASSWORD.lower() in {"", "admin", "change-me"}:
  raise RuntimeError("Set ADMIN_PASSWORD_HASH (recommended) or a non-default ADMIN_PASSWORD in .env.")

engine_kwargs = {"future": True, "pool_pre_ping": True}
if not DATABASE_URL.startswith("sqlite"):
  engine_kwargs.update(
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_recycle=DB_POOL_RECYCLE,
    pool_timeout=DB_POOL_TIMEOUT,
  )
engine = create_engine(DATABASE_URL, **engine_kwargs)
app = FastAPI(title="Pilot Production API")


def _clean(value: Any, max_len: int = 400) -> str:
  return str(value or "").strip()[:max_len]


def _rate(value: Any, default: float = 0.0) -> float:
  try:
    numeric = float(value)
  except (TypeError, ValueError):
    return float(default)
  return round(max(0.0, numeric), 2)


def _now() -> str:
  return datetime.now(timezone.utc).isoformat()


def _time(value: Any) -> str:
  raw = _clean(value, 10)
  return raw if re.fullmatch(r"\d{2}:\d{2}", raw) else ""


def _password_policy_error(password: str, min_length: int = MIN_STAFF_PASSWORD_LENGTH) -> str:
  cleaned = _clean(password, 400)
  if len(cleaned) < min_length:
    return f"Password must be at least {min_length} characters."
  if cleaned.lower() == cleaned or cleaned.upper() == cleaned:
    return "Password must include both lowercase and uppercase letters."
  if not any(char.isdigit() for char in cleaned):
    return "Password must include at least one number."
  return ""


def _hash_password(password: str) -> str:
  cleaned = _clean(password, 400)
  salt = secrets.token_hex(16)
  iterations = 210_000
  digest = hashlib.pbkdf2_hmac("sha256", cleaned.encode("utf-8"), bytes.fromhex(salt), iterations).hex()
  return "pbkdf2_sha256$" + str(iterations) + "$" + salt + "$" + digest


def _verify_password(password: str, encoded: str) -> bool:
  cleaned_password = _clean(password, 400)
  cleaned_encoded = _clean(encoded, 1200)
  if not cleaned_password or not cleaned_encoded:
    return False
  normalized_encoded = cleaned_encoded.replace("$$", "$")
  if not normalized_encoded.startswith("pbkdf2_sha256$"):
    return hmac.compare_digest(cleaned_password, cleaned_encoded)
  parts = normalized_encoded.split("$", 3)
  if len(parts) != 4:
    return False
  _, it_raw, salt_hex, digest_hex = parts
  try:
    iterations = int(it_raw)
    salt = bytes.fromhex(salt_hex)
  except ValueError:
    return False
  computed = hashlib.pbkdf2_hmac("sha256", cleaned_password.encode("utf-8"), salt, iterations).hex()
  return hmac.compare_digest(computed, digest_hex)


if AUTH_REQUIRED and not ADMIN_PASSWORD_HASH:
  admin_password_error = _password_policy_error(ADMIN_PASSWORD, max(12, MIN_STAFF_PASSWORD_LENGTH))
  if admin_password_error:
    raise RuntimeError("ADMIN_PASSWORD is too weak. " + admin_password_error)


def _token(payload: dict[str, Any]) -> str:
  body = dict(payload)
  body["exp"] = int((datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)).timestamp())
  payload_b64 = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
  sig = hmac.new(TOKEN_SECRET.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
  sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
  return payload_b64 + "." + sig_b64


def _verify_token(raw: str) -> dict[str, Any] | None:
  token = _clean(raw, 6000)
  if "." not in token:
    return None
  payload_b64, sig_b64 = token.split(".", 1)
  expected = hmac.new(TOKEN_SECRET.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
  try:
    padding = "=" * ((4 - (len(sig_b64) % 4)) % 4)
    provided = base64.urlsafe_b64decode(sig_b64 + padding)
  except Exception:
    return None
  if not hmac.compare_digest(expected, provided):
    return None
  try:
    padding = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8"))
  except Exception:
    return None
  if int(payload.get("exp") or 0) <= int(datetime.now(timezone.utc).timestamp()):
    return None
  return payload if isinstance(payload, dict) else None


def _auth(request: Request, roles: set[str] | None = None) -> dict[str, Any] | None:
  header = _clean(request.headers.get("Authorization"), 5000)
  payload = None
  if header.lower().startswith("bearer "):
    payload = _verify_token(header[7:].strip())
  if AUTH_REQUIRED and roles:
    if not payload:
      raise HTTPException(status_code=401, detail="Missing or invalid token.")
    if _clean(payload.get("role"), 40).lower() not in roles:
      raise HTTPException(status_code=403, detail="Insufficient permissions.")
  return payload


def _client_key(request: Request, scope: str) -> str:
  forwarded = _clean(request.headers.get("x-forwarded-for"), 200)
  if forwarded:
    ip = forwarded.split(",", 1)[0].strip()
  else:
    ip = request.client.host if request.client and request.client.host else "unknown"
  return scope + ":" + _clean(ip, 120)


def _login_limited(request: Request, scope: str) -> bool:
  if LOGIN_RATE_LIMIT <= 0:
    return False
  key = _client_key(request, scope)
  now = datetime.now(timezone.utc).timestamp()
  window_start = now - float(LOGIN_RATE_WINDOW_SEC)
  history = [ts for ts in _FAILED_LOGIN.get(key, []) if ts >= window_start]
  _FAILED_LOGIN[key] = history
  return len(history) >= LOGIN_RATE_LIMIT


def _record_login_failure(request: Request, scope: str) -> None:
  key = _client_key(request, scope)
  now = datetime.now(timezone.utc).timestamp()
  history = [ts for ts in _FAILED_LOGIN.get(key, []) if ts >= now - float(LOGIN_RATE_WINDOW_SEC)]
  history.append(now)
  _FAILED_LOGIN[key] = history


def _clear_login_failures(request: Request, scope: str) -> None:
  _FAILED_LOGIN.pop(_client_key(request, scope), None)


def _booking_limited(request: Request) -> bool:
  if BOOKING_RATE_LIMIT <= 0:
    return False
  key = _client_key(request, "booking_submit")
  now = datetime.now(timezone.utc).timestamp()
  window_start = now - float(BOOKING_RATE_WINDOW_SEC)
  history = [ts for ts in _BOOKING_REQUESTS.get(key, []) if ts >= window_start]
  _BOOKING_REQUESTS[key] = history
  return len(history) >= BOOKING_RATE_LIMIT


def _record_booking_request(request: Request) -> None:
  key = _client_key(request, "booking_submit")
  now = datetime.now(timezone.utc).timestamp()
  history = [ts for ts in _BOOKING_REQUESTS.get(key, []) if ts >= now - float(BOOKING_RATE_WINDOW_SEC)]
  history.append(now)
  _BOOKING_REQUESTS[key] = history


def _enforce_staff_site(payload: dict[str, Any] | None, site: str) -> None:
  if not AUTH_REQUIRED:
    return
  role = _clean((payload or {}).get("role"), 40).lower()
  if role != "staff":
    return
  token_site = _clean((payload or {}).get("site"), 120)
  if token_site and token_site != _clean(site, 120):
    raise HTTPException(status_code=403, detail="Staff token is not valid for this site.")


def _rows(result) -> list[dict[str, Any]]:
  return [dict(row._mapping) for row in result]


def _sanitize_staff(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": _clean(row.get("id"), 180),
    "site": _clean(row.get("site"), 120),
    "name": _clean(row.get("name"), 120),
    "hourlyRate": _rate(row.get("hourly_rate"), 0.0),
    "createdAt": _clean(row.get("created_at"), 60),
    "updatedAt": _clean(row.get("updated_at"), 60),
  }


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
  response = await call_next(request)
  response.headers.setdefault("X-Content-Type-Options", "nosniff")
  response.headers.setdefault("X-Frame-Options", "DENY")
  response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
  response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
  response.headers.setdefault("Cache-Control", "no-store")
  return response


@app.on_event("startup")
def startup() -> None:
  with engine.begin() as conn:
    conn.execute(text("CREATE TABLE IF NOT EXISTS bookings (id TEXT PRIMARY KEY, site TEXT NOT NULL, booking_number INTEGER NOT NULL, business_name TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'restaurant', guests INTEGER NOT NULL DEFAULT 2, service_type TEXT NOT NULL DEFAULT '', date TEXT NOT NULL, time TEXT NOT NULL, full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_bookings_site_date ON bookings (site, date, time)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_bookings_site_created ON bookings (site, created_at)"))
    conn.execute(text("CREATE TABLE IF NOT EXISTS staff_members (id TEXT PRIMARY KEY, site TEXT NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL, hourly_rate REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_staff_site_name ON staff_members (site, name)"))
    conn.execute(text("CREATE TABLE IF NOT EXISTS availability_entries (id TEXT PRIMARY KEY, site TEXT NOT NULL, staff_id TEXT NOT NULL, date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_availability_site_date ON availability_entries (site, date)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_availability_site_staff_date ON availability_entries (site, staff_id, date)"))


@app.get("/api/health")
def api_health() -> dict[str, Any]:
  return {"ok": True}


@app.post("/api/admin/login")
async def api_admin_login(request: Request) -> dict[str, Any]:
  if _login_limited(request, "admin_login"):
    raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
  body = await request.json()
  username = _clean((body or {}).get("username"), 120)
  password = _clean((body or {}).get("password"), 400)
  if not username or not password:
    raise HTTPException(status_code=400, detail="username and password are required.")
  valid = hmac.compare_digest(username.lower(), ADMIN_USERNAME.lower()) and (
    (_verify_password(password, ADMIN_PASSWORD_HASH) if ADMIN_PASSWORD_HASH else False)
    or (ADMIN_PASSWORD and hmac.compare_digest(password, ADMIN_PASSWORD))
  )
  if not valid:
    _record_login_failure(request, "admin_login")
    raise HTTPException(status_code=401, detail="Invalid username or password.")
  _clear_login_failures(request, "admin_login")
  return {"ok": True, "admin": {"username": username}, "token": _token({"sub": username, "role": "admin"})}


@app.post("/api/staff/login")
async def api_staff_login(request: Request) -> dict[str, Any]:
  if _login_limited(request, "staff_login"):
    raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
  body = await request.json()
  site = _clean((body or {}).get("site"), 120)
  name = _clean((body or {}).get("name"), 120)
  password = _clean((body or {}).get("password"), 400)
  if not site or not name or not password:
    raise HTTPException(status_code=400, detail="site, name and password are required.")
  with engine.begin() as conn:
    row = conn.execute(text("SELECT * FROM staff_members WHERE site = :site AND LOWER(name) = LOWER(:name) LIMIT 1"), {"site": site, "name": name}).first()
  if not row or not _verify_password(password, _clean(row._mapping.get("password_hash"), 1200)):
    _record_login_failure(request, "staff_login")
    raise HTTPException(status_code=401, detail="Invalid name or password.")
  _clear_login_failures(request, "staff_login")
  staff = _sanitize_staff(dict(row._mapping))
  return {"ok": True, "staffMember": staff, "token": _token({"sub": staff["id"], "role": "staff", "site": site})}


@app.get("/api/bookings")
def api_get_bookings(request: Request, site: str = "") -> dict[str, Any]:
  payload = _auth(request, {"admin", "staff"}) or {}
  cleaned_site = _clean(site, 120)
  if not cleaned_site:
    raise HTTPException(status_code=400, detail="Missing site.")
  _enforce_staff_site(payload, cleaned_site)
  with engine.begin() as conn:
    rows = _rows(conn.execute(text("SELECT * FROM bookings WHERE site = :site ORDER BY date ASC, time ASC, booking_number ASC"), {"site": cleaned_site}))
  bookings = [{"id": _clean(r.get("id"), 180), "site": _clean(r.get("site"), 120), "bookingNumber": int(r.get("booking_number") or 0), "businessName": _clean(r.get("business_name"), 180), "mode": _clean(r.get("mode"), 30), "guests": int(r.get("guests") or 2), "serviceType": _clean(r.get("service_type"), 120), "date": _clean(r.get("date"), 20), "time": _clean(r.get("time"), 20), "fullName": _clean(r.get("full_name"), 120), "email": _clean(r.get("email"), 160), "phone": _clean(r.get("phone"), 80), "notes": _clean(r.get("notes"), 1000), "createdAt": _clean(r.get("created_at"), 60)} for r in rows]
  return {"ok": True, "bookings": bookings}


@app.post("/api/bookings")
async def api_post_bookings(request: Request) -> JSONResponse:
  if _booking_limited(request):
    raise HTTPException(status_code=429, detail="Too many booking attempts. Try again later.")
  body = await request.json()
  site = _clean((body or {}).get("site"), 120)
  full_name = _clean((body or {}).get("fullName"), 120)
  email = _clean((body or {}).get("email"), 160)
  phone = _clean((body or {}).get("phone"), 80)
  date_value = _clean((body or {}).get("date"), 20)
  time_value = _time((body or {}).get("time"))
  if not site or not full_name or not email or not phone or not date_value or not time_value:
    raise HTTPException(status_code=400, detail="site, fullName, email, phone, date and time are required.")
  _record_booking_request(request)
  with engine.begin() as conn:
    max_row = conn.execute(text("SELECT COALESCE(MAX(booking_number), 0) AS max_num FROM bookings WHERE site = :site"), {"site": site}).first()
    booking_number = int((max_row._mapping.get("max_num") if max_row else 0) or 0) + 1
    booking_id = f"{site}-booking-{booking_number}"
    conn.execute(text("INSERT INTO bookings (id, site, booking_number, business_name, mode, guests, service_type, date, time, full_name, email, phone, notes, created_at) VALUES (:id, :site, :booking_number, :business_name, :mode, :guests, :service_type, :date, :time, :full_name, :email, :phone, :notes, :created_at)"), {"id": booking_id, "site": site, "booking_number": booking_number, "business_name": _clean((body or {}).get("businessName"), 180), "mode": _clean((body or {}).get("mode"), 30) or "restaurant", "guests": max(1, min(20, int((body or {}).get("guests") or 2))), "service_type": _clean((body or {}).get("serviceType"), 120), "date": date_value, "time": time_value, "full_name": full_name, "email": email, "phone": phone, "notes": _clean((body or {}).get("notes"), 1000), "created_at": _now()})
  return JSONResponse({"ok": True, "booking": {"id": booking_id, "site": site, "bookingNumber": booking_number}}, status_code=201)


@app.put("/api/bookings/{booking_id}")
async def api_put_bookings(booking_id: str, request: Request) -> dict[str, Any]:
  _auth(request, {"admin"})
  body = await request.json()
  site = _clean((body or {}).get("site"), 120)
  if not site:
    raise HTTPException(status_code=400, detail="Missing site.")
  with engine.begin() as conn:
    result = conn.execute(text("UPDATE bookings SET date = :date, time = :time, full_name = :full_name, email = :email, phone = :phone, guests = :guests, service_type = :service_type, notes = :notes WHERE id = :id AND site = :site"), {"id": _clean(booking_id, 180), "site": site, "date": _clean((body or {}).get("date"), 20), "time": _time((body or {}).get("time")), "full_name": _clean((body or {}).get("fullName"), 120), "email": _clean((body or {}).get("email"), 160), "phone": _clean((body or {}).get("phone"), 80), "guests": max(1, min(20, int((body or {}).get("guests") or 2))), "service_type": _clean((body or {}).get("serviceType"), 120), "notes": _clean((body or {}).get("notes"), 1000)})
    if result.rowcount <= 0:
      raise HTTPException(status_code=404, detail="Booking not found.")
  return {"ok": True}


@app.delete("/api/bookings/{booking_id}")
def api_delete_bookings(booking_id: str, request: Request, site: str = "") -> dict[str, Any]:
  _auth(request, {"admin"})
  cleaned_site = _clean(site, 120)
  if not cleaned_site:
    raise HTTPException(status_code=400, detail="Missing site.")
  with engine.begin() as conn:
    result = conn.execute(text("DELETE FROM bookings WHERE id = :id AND site = :site"), {"id": _clean(booking_id, 180), "site": cleaned_site})
    if result.rowcount <= 0:
      raise HTTPException(status_code=404, detail="Booking not found.")
  return {"ok": True, "removed": 1}


@app.get("/api/staff")
def api_get_staff(request: Request, site: str = "") -> dict[str, Any]:
  _auth(request, {"admin"})
  cleaned_site = _clean(site, 120)
  if not cleaned_site:
    raise HTTPException(status_code=400, detail="Missing site.")
  with engine.begin() as conn:
    rows = _rows(conn.execute(text("SELECT * FROM staff_members WHERE site = :site ORDER BY LOWER(name)"), {"site": cleaned_site}))
  return {"ok": True, "staffMembers": [_sanitize_staff(r) for r in rows]}


@app.post("/api/staff")
async def api_post_staff(request: Request) -> JSONResponse:
  _auth(request, {"admin"})
  body = await request.json()
  site = _clean((body or {}).get("site"), 120)
  name = _clean((body or {}).get("name"), 120)
  password = _clean((body or {}).get("password"), 400)
  if not site or not name or not password:
    raise HTTPException(status_code=400, detail="site, name and password are required.")
  password_error = _password_policy_error(password)
  if password_error:
    raise HTTPException(status_code=400, detail=password_error)
  staff_id = f"{site}-staff-{uuid.uuid4()}"
  now = _now()
  with engine.begin() as conn:
    duplicate = conn.execute(text("SELECT id FROM staff_members WHERE site = :site AND LOWER(name) = LOWER(:name) LIMIT 1"), {"site": site, "name": name}).first()
    if duplicate:
      raise HTTPException(status_code=409, detail="Staff name already exists for this site.")
    conn.execute(text("INSERT INTO staff_members (id, site, name, password_hash, hourly_rate, created_at, updated_at) VALUES (:id, :site, :name, :password_hash, :hourly_rate, :created_at, :updated_at)"), {"id": staff_id, "site": site, "name": name, "password_hash": _hash_password(password), "hourly_rate": _rate((body or {}).get("hourlyRate"), 0.0), "created_at": now, "updated_at": now})
    row = conn.execute(text("SELECT * FROM staff_members WHERE id = :id"), {"id": staff_id}).first()
  return JSONResponse({"ok": True, "staffMember": _sanitize_staff(dict(row._mapping))}, status_code=201)


@app.put("/api/staff/{staff_id}")
async def api_put_staff(staff_id: str, request: Request) -> dict[str, Any]:
  _auth(request, {"admin"})
  body = await request.json()
  site = _clean((body or {}).get("site"), 120)
  if not site:
    raise HTTPException(status_code=400, detail="Missing site.")
  with engine.begin() as conn:
    current = conn.execute(text("SELECT * FROM staff_members WHERE id = :id AND site = :site LIMIT 1"), {"id": _clean(staff_id, 180), "site": site}).first()
    if not current:
      raise HTTPException(status_code=404, detail="Staff member not found.")
    name = _clean((body or {}).get("name"), 120) or _clean(current._mapping.get("name"), 120)
    conn.execute(text("UPDATE staff_members SET name = :name, hourly_rate = :hourly_rate, updated_at = :updated_at WHERE id = :id AND site = :site"), {"id": _clean(staff_id, 180), "site": site, "name": name, "hourly_rate": _rate((body or {}).get("hourlyRate"), _rate(current._mapping.get("hourly_rate"), 0.0)), "updated_at": _now()})
    new_password = _clean((body or {}).get("password"), 400)
    if new_password:
      password_error = _password_policy_error(new_password)
      if password_error:
        raise HTTPException(status_code=400, detail=password_error)
      conn.execute(text("UPDATE staff_members SET password_hash = :password_hash, updated_at = :updated_at WHERE id = :id AND site = :site"), {"id": _clean(staff_id, 180), "site": site, "password_hash": _hash_password(new_password), "updated_at": _now()})
    row = conn.execute(text("SELECT * FROM staff_members WHERE id = :id"), {"id": _clean(staff_id, 180)}).first()
  return {"ok": True, "staffMember": _sanitize_staff(dict(row._mapping))}


@app.delete("/api/staff/{staff_id}")
def api_delete_staff(staff_id: str, request: Request, site: str = "") -> dict[str, Any]:
  _auth(request, {"admin"})
  cleaned_site = _clean(site, 120)
  if not cleaned_site:
    raise HTTPException(status_code=400, detail="Missing site.")
  with engine.begin() as conn:
    removed_staff = conn.execute(text("DELETE FROM staff_members WHERE id = :id AND site = :site"), {"id": _clean(staff_id, 180), "site": cleaned_site}).rowcount
    removed_availability = conn.execute(text("DELETE FROM availability_entries WHERE staff_id = :staff_id AND site = :site"), {"staff_id": _clean(staff_id, 180), "site": cleaned_site}).rowcount
    if removed_staff <= 0:
      raise HTTPException(status_code=404, detail="Staff member not found.")
  return {"ok": True, "removedStaff": int(removed_staff), "removedAvailability": int(removed_availability)}


@app.get("/api/staff/availability")
def api_get_availability(request: Request, site: str = "", staffId: str = "") -> dict[str, Any]:
  payload = _auth(request, {"admin", "staff"}) or {}
  cleaned_site = _clean(site, 120)
  if not cleaned_site:
    raise HTTPException(status_code=400, detail="Missing site.")
  _enforce_staff_site(payload, cleaned_site)
  role = _clean(payload.get("role"), 40).lower()
  effective_staff_id = _clean(staffId, 180)
  if AUTH_REQUIRED and role == "staff":
    effective_staff_id = _clean(payload.get("sub"), 180)
  with engine.begin() as conn:
    query = "SELECT * FROM availability_entries WHERE site = :site"
    params = {"site": cleaned_site}
    if effective_staff_id:
      query += " AND staff_id = :staff_id"
      params["staff_id"] = effective_staff_id
    query += " ORDER BY date ASC, start_time ASC"
    entries = _rows(conn.execute(text(query), params))
    staff_rows = _rows(conn.execute(text("SELECT id, name FROM staff_members WHERE site = :site"), {"site": cleaned_site}))
  names = {_clean(r.get("id"), 180): _clean(r.get("name"), 120) for r in staff_rows}
  mapped = [{"id": _clean(e.get("id"), 180), "site": _clean(e.get("site"), 120), "staffId": _clean(e.get("staff_id"), 180), "staffName": names.get(_clean(e.get("staff_id"), 180), ""), "date": _clean(e.get("date"), 20), "startTime": _clean(e.get("start_time"), 10), "endTime": _clean(e.get("end_time"), 10), "notes": _clean(e.get("notes"), 500), "createdAt": _clean(e.get("created_at"), 60), "updatedAt": _clean(e.get("updated_at"), 60)} for e in entries]
  return {"ok": True, "availabilityEntries": mapped}


@app.post("/api/staff/availability")
async def api_post_availability(request: Request) -> JSONResponse:
  payload = _auth(request, {"admin", "staff"}) or {}
  body = await request.json()
  site = _clean((body or {}).get("site"), 120)
  staff_id = _clean((body or {}).get("staffId"), 180)
  if AUTH_REQUIRED and _clean(payload.get("role"), 40).lower() == "staff":
    staff_id = _clean(payload.get("sub"), 180)
  _enforce_staff_site(payload, site)
  date_value = _clean((body or {}).get("date"), 20)
  start_time = _time((body or {}).get("startTime"))
  end_time = _time((body or {}).get("endTime"))
  if not site or not staff_id or not date_value or not start_time or not end_time:
    raise HTTPException(status_code=400, detail="site, staffId, date, startTime and endTime are required.")
  entry_id = f"{site}-availability-{uuid.uuid4()}"
  now = _now()
  with engine.begin() as conn:
    conn.execute(text("INSERT INTO availability_entries (id, site, staff_id, date, start_time, end_time, notes, created_at, updated_at) VALUES (:id, :site, :staff_id, :date, :start_time, :end_time, :notes, :created_at, :updated_at)"), {"id": entry_id, "site": site, "staff_id": staff_id, "date": date_value, "start_time": start_time, "end_time": end_time, "notes": _clean((body or {}).get("notes"), 500), "created_at": now, "updated_at": now})
    name_row = conn.execute(text("SELECT name FROM staff_members WHERE id = :id LIMIT 1"), {"id": staff_id}).first()
  return JSONResponse({"ok": True, "availabilityEntry": {"id": entry_id, "site": site, "staffId": staff_id, "staffName": _clean(name_row._mapping.get("name") if name_row else "", 120), "date": date_value, "startTime": start_time, "endTime": end_time, "notes": _clean((body or {}).get("notes"), 500), "createdAt": now, "updatedAt": now}}, status_code=201)


@app.put("/api/staff/availability/{entry_id}")
async def api_put_availability(entry_id: str, request: Request) -> dict[str, Any]:
  payload = _auth(request, {"admin", "staff"}) or {}
  body = await request.json()
  site = _clean((body or {}).get("site"), 120)
  if not site:
    raise HTTPException(status_code=400, detail="Missing site.")
  _enforce_staff_site(payload, site)
  with engine.begin() as conn:
    current = conn.execute(
      text("SELECT staff_id FROM availability_entries WHERE id = :id AND site = :site LIMIT 1"),
      {"id": _clean(entry_id, 180), "site": site},
    ).first()
    if not current:
      raise HTTPException(status_code=404, detail="Availability entry not found.")
    if AUTH_REQUIRED and _clean(payload.get("role"), 40).lower() == "staff":
      if _clean(payload.get("sub"), 180) != _clean(current._mapping.get("staff_id"), 180):
        raise HTTPException(status_code=403, detail="You can only update your own availability.")
    result = conn.execute(text("UPDATE availability_entries SET start_time = :start_time, end_time = :end_time, notes = :notes, updated_at = :updated_at WHERE id = :id AND site = :site"), {"id": _clean(entry_id, 180), "site": site, "start_time": _time((body or {}).get("startTime")), "end_time": _time((body or {}).get("endTime")), "notes": _clean((body or {}).get("notes"), 500), "updated_at": _now()})
    if result.rowcount <= 0:
      raise HTTPException(status_code=404, detail="Availability entry not found.")
  return {"ok": True}


@app.delete("/api/staff/availability/{entry_id}")
def api_delete_availability(entry_id: str, request: Request, site: str = "") -> dict[str, Any]:
  payload = _auth(request, {"admin", "staff"}) or {}
  cleaned_site = _clean(site, 120)
  if not cleaned_site:
    raise HTTPException(status_code=400, detail="Missing site.")
  _enforce_staff_site(payload, cleaned_site)
  with engine.begin() as conn:
    current = conn.execute(
      text("SELECT staff_id FROM availability_entries WHERE id = :id AND site = :site LIMIT 1"),
      {"id": _clean(entry_id, 180), "site": cleaned_site},
    ).first()
    if not current:
      raise HTTPException(status_code=404, detail="Availability entry not found.")
    if AUTH_REQUIRED and _clean(payload.get("role"), 40).lower() == "staff":
      if _clean(payload.get("sub"), 180) != _clean(current._mapping.get("staff_id"), 180):
        raise HTTPException(status_code=403, detail="You can only remove your own availability.")
    result = conn.execute(text("DELETE FROM availability_entries WHERE id = :id AND site = :site"), {"id": _clean(entry_id, 180), "site": cleaned_site})
    if result.rowcount <= 0:
      raise HTTPException(status_code=404, detail="Availability entry not found.")
  return {"ok": True, "removed": 1}


@app.get("/api/staff/schedule")
def api_get_schedule(request: Request, site: str = "", weekStart: str = "") -> dict[str, Any]:
  payload = _auth(request, {"admin", "staff"}) or {}
  cleaned_site = _clean(site, 120)
  if not cleaned_site:
    raise HTTPException(status_code=400, detail="Missing site.")
  _enforce_staff_site(payload, cleaned_site)
  try:
    seed = datetime.fromisoformat((_clean(weekStart, 20) or datetime.now().date().isoformat()) + "T00:00:00").date()
  except ValueError:
    seed = datetime.now().date()
  week_start = seed - timedelta(days=seed.weekday())
  day_keys = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
  with engine.begin() as conn:
    staff_rows = _rows(conn.execute(text("SELECT id, name FROM staff_members WHERE site = :site ORDER BY LOWER(name)"), {"site": cleaned_site}))
    entries = _rows(conn.execute(text("SELECT * FROM availability_entries WHERE site = :site AND date >= :start_date AND date <= :end_date ORDER BY date ASC, start_time ASC"), {"site": cleaned_site, "start_date": day_keys[0], "end_date": day_keys[-1]}))
  rows = []
  for staff in staff_rows:
    sid = _clean(staff.get("id"), 180)
    days = []
    for day_key in day_keys:
      day_slots = [{"id": _clean(e.get("id"), 180), "date": _clean(e.get("date"), 20), "startTime": _clean(e.get("start_time"), 10), "endTime": _clean(e.get("end_time"), 10), "notes": _clean(e.get("notes"), 500), "staffId": sid, "staffName": _clean(staff.get("name"), 120)} for e in entries if _clean(e.get("staff_id"), 180) == sid and _clean(e.get("date"), 20) == day_key]
      days.append(day_slots)
    rows.append({"staffId": sid, "name": _clean(staff.get("name"), 120), "days": days})
  return {"ok": True, "weekStart": week_start.isoformat(), "days": day_keys, "rows": rows}


@app.get("/")
@app.get("/admin")
@app.get("/staff")
def serve_site() -> FileResponse:
  if not SITE_FILE.exists():
    raise HTTPException(status_code=404, detail="Generated site file not found.")
  return FileResponse(SITE_FILE)


@app.get("/{asset_path:path}")
def serve_assets(asset_path: str):
  safe = _clean(asset_path, 300)
  if safe.startswith("api/"):
    raise HTTPException(status_code=404, detail="Not found")
  candidate = (APP_ROOT / safe).resolve()
  if candidate.exists() and candidate.is_file() and APP_ROOT in candidate.parents:
    return FileResponse(candidate)
  if SITE_FILE.exists():
    return FileResponse(SITE_FILE)
  return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
