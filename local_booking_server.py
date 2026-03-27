from __future__ import annotations

import argparse
import json
import threading
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT_DIR = Path(__file__).resolve().parent
DB_DIR = ROOT_DIR / "database"
DB_FILE = DB_DIR / "bookings.json"
DB_LOCK = threading.Lock()


def _default_db() -> dict:
  return {
    "nextBookingNumber": 1,
    "nextStaffId": 1,
    "nextAvailabilityId": 1,
    "bookings": [],
    "staffMembers": [],
    "availabilityEntries": [],
    "staffWeeklyChangeLog": [],
  }


def _read_db() -> dict:
  DB_DIR.mkdir(parents=True, exist_ok=True)
  if not DB_FILE.exists():
    data = _default_db()
    DB_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data
  try:
    data = json.loads(DB_FILE.read_text(encoding="utf-8"))
  except json.JSONDecodeError:
    data = _default_db()
  if not isinstance(data, dict):
    data = _default_db()
  defaults = _default_db()
  for key, value in defaults.items():
    if key not in data:
      data[key] = value
  if not isinstance(data.get("bookings"), list):
    data["bookings"] = []
  if not isinstance(data.get("staffMembers"), list):
    data["staffMembers"] = []
  if not isinstance(data.get("availabilityEntries"), list):
    data["availabilityEntries"] = []
  if not isinstance(data.get("staffWeeklyChangeLog"), list):
    data["staffWeeklyChangeLog"] = []
  for member in data.get("staffMembers", []):
    if isinstance(member, dict):
      member["hourlyRate"] = _clean_hourly_rate(member.get("hourlyRate"), 0.0)
  for number_key in ["nextBookingNumber", "nextStaffId", "nextAvailabilityId"]:
    if not isinstance(data.get(number_key), int):
      data[number_key] = 1
  return data


def _write_db(data: dict) -> None:
  DB_DIR.mkdir(parents=True, exist_ok=True)
  DB_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _clean_text(value: object, max_len: int = 400) -> str:
  return str(value or "").strip()[:max_len]


def _clean_hourly_rate(value: object, default: float = 0.0) -> float:
  try:
    numeric = float(value)
  except (TypeError, ValueError):
    return float(default)
  if numeric < 0:
    numeric = 0.0
  return round(numeric, 2)


def _parse_iso_date(raw: str) -> date | None:
  value = _clean_text(raw, 20)
  try:
    return date.fromisoformat(value)
  except ValueError:
    return None


def _start_of_week(day: date) -> date:
  return day - timedelta(days=day.weekday())


def _week_dates(week_start: date) -> list[str]:
  return [(week_start + timedelta(days=offset)).isoformat() for offset in range(7)]


def _week_key(day: date) -> str:
  return _start_of_week(day).isoformat()


def _resolve_site_file(explicit_site_file: str | None) -> Path | None:
  if explicit_site_file:
    explicit = (ROOT_DIR / explicit_site_file).resolve()
    if explicit.exists() and explicit.is_file():
      return explicit

  candidates = [
    "generated-site.html",
    "site.html",
    "pilot-site.html",
    "export.html",
  ]
  for candidate in candidates:
    candidate_path = ROOT_DIR / candidate
    if candidate_path.exists() and candidate_path.is_file():
      return candidate_path

  html_files = sorted(
    [path for path in ROOT_DIR.glob("*.html") if path.name.lower() != "index.html"],
    key=lambda path: path.stat().st_mtime,
    reverse=True,
  )
  if html_files:
    return html_files[0]
  editor_fallback = ROOT_DIR / "index.html"
  if editor_fallback.exists():
    return editor_fallback
  return None


class BookingHandler(SimpleHTTPRequestHandler):
  server_version = "LocalBookingHTTP/2.0"

  def __init__(self, *args, directory: str | None = None, site_file: str | None = None, **kwargs):
    self._site_file = site_file
    super().__init__(*args, directory=directory, **kwargs)

  def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def _read_json_body(self) -> dict | None:
    try:
      content_length = int(self.headers.get("Content-Length", "0"))
    except ValueError:
      content_length = 0
    raw_body = self.rfile.read(max(0, content_length))
    try:
      payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
      return None
    if not isinstance(payload, dict):
      return None
    return payload

  def _serve_html_file(self, path: Path) -> None:
    data = path.read_bytes()
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)

  def _serve_site_page(self) -> None:
    site_file = _resolve_site_file(self._site_file)
    if not site_file:
      self.send_error(HTTPStatus.NOT_FOUND, "No site HTML file found.")
      return
    self._serve_html_file(site_file)

  def _serve_generator_page(self) -> None:
    generator_file = ROOT_DIR / "index.html"
    if not generator_file.exists():
      self.send_error(HTTPStatus.NOT_FOUND, "Generator index.html not found.")
      return
    self._serve_html_file(generator_file)

  def _handle_get_bookings(self, parsed) -> None:
    query = parse_qs(parsed.query)
    site_filter = _clean_text((query.get("site") or [""])[0], 120)
    with DB_LOCK:
      data = _read_db()
      bookings = data.get("bookings", [])
    if site_filter:
      bookings = [booking for booking in bookings if _clean_text(booking.get("site"), 120) == site_filter]
    bookings = sorted(
      bookings,
      key=lambda b: (
        _clean_text(b.get("date"), 20),
        _clean_text(b.get("time"), 20),
        int(b.get("bookingNumber") or 0),
      ),
    )
    self._send_json({"ok": True, "bookings": bookings})

  def _handle_post_booking(self) -> None:
    payload = self._read_json_body()
    if payload is None:
      self._send_json({"ok": False, "error": "Invalid JSON payload."}, status=HTTPStatus.BAD_REQUEST)
      return

    site = _clean_text(payload.get("site"), 120)
    full_name = _clean_text(payload.get("fullName"), 120)
    email = _clean_text(payload.get("email"), 160)
    phone = _clean_text(payload.get("phone"), 80)
    booking_date = _clean_text(payload.get("date"), 20)
    booking_time = _clean_text(payload.get("time"), 20)

    required_fields = {
      "site": site,
      "fullName": full_name,
      "email": email,
      "phone": phone,
      "date": booking_date,
      "time": booking_time,
    }
    missing = [field for field, value in required_fields.items() if not value]
    if missing:
      self._send_json(
        {"ok": False, "error": "Missing required field(s): " + ", ".join(missing)},
        status=HTTPStatus.BAD_REQUEST,
      )
      return

    mode = _clean_text(payload.get("mode"), 30) or "restaurant"
    try:
      guests = int(payload.get("guests", 2))
    except (TypeError, ValueError):
      guests = 2
    guests = max(1, min(20, guests))
    service_type = _clean_text(payload.get("serviceType"), 120)
    notes = _clean_text(payload.get("notes"), 1000)
    business_name = _clean_text(payload.get("businessName"), 180)
    created_at = datetime.now(timezone.utc).isoformat()

    with DB_LOCK:
      data = _read_db()
      booking_number = int(data.get("nextBookingNumber") or 1)
      booking = {
        "id": f"{site}-booking-{booking_number}",
        "site": site,
        "bookingNumber": booking_number,
        "businessName": business_name,
        "mode": mode,
        "guests": guests,
        "serviceType": service_type,
        "date": booking_date,
        "time": booking_time,
        "fullName": full_name,
        "email": email,
        "phone": phone,
        "notes": notes,
        "createdAt": created_at,
      }
      data.setdefault("bookings", []).append(booking)
      data["nextBookingNumber"] = booking_number + 1
      _write_db(data)

    self._send_json({"ok": True, "booking": booking}, status=HTTPStatus.CREATED)

  def _handle_put_booking(self, booking_id: str) -> None:
    payload = self._read_json_body()
    if payload is None:
      self._send_json({"ok": False, "error": "Invalid JSON payload."}, status=HTTPStatus.BAD_REQUEST)
      return

    site = _clean_text(payload.get("site"), 120)
    booking_date = _clean_text(payload.get("date"), 20)
    booking_time = _clean_text(payload.get("time"), 10)
    full_name = _clean_text(payload.get("fullName"), 120)
    email = _clean_text(payload.get("email"), 160)
    phone = _clean_text(payload.get("phone"), 80)
    service_type = _clean_text(payload.get("serviceType"), 120)
    mode = _clean_text(payload.get("mode"), 30)
    notes = _clean_text(payload.get("notes"), 1000)
    if not site or not booking_date or not booking_time:
      self._send_json({"ok": False, "error": "site, date and time are required."}, status=HTTPStatus.BAD_REQUEST)
      return

    if not _parse_iso_date(booking_date):
      self._send_json({"ok": False, "error": "Invalid date format. Use YYYY-MM-DD."}, status=HTTPStatus.BAD_REQUEST)
      return
    if len(booking_time) != 5 or ":" not in booking_time:
      self._send_json({"ok": False, "error": "Invalid time format. Use HH:MM."}, status=HTTPStatus.BAD_REQUEST)
      return
    if "fullName" in payload and not full_name:
      self._send_json({"ok": False, "error": "fullName cannot be empty."}, status=HTTPStatus.BAD_REQUEST)
      return
    if "email" in payload and not email:
      self._send_json({"ok": False, "error": "email cannot be empty."}, status=HTTPStatus.BAD_REQUEST)
      return
    if "phone" in payload and not phone:
      self._send_json({"ok": False, "error": "phone cannot be empty."}, status=HTTPStatus.BAD_REQUEST)
      return
    guests = None
    if "guests" in payload:
      try:
        guests = int(payload.get("guests", 2))
      except (TypeError, ValueError):
        guests = 2
      guests = max(1, min(20, guests))

    with DB_LOCK:
      data = _read_db()
      bookings = data.get("bookings", [])
      target = next(
        (
          booking
          for booking in bookings
          if _clean_text(booking.get("id"), 180) == booking_id and _clean_text(booking.get("site"), 120) == site
        ),
        None,
      )
      if not target:
        self._send_json({"ok": False, "error": "Booking not found."}, status=HTTPStatus.NOT_FOUND)
        return

      target["date"] = booking_date
      target["time"] = booking_time
      if "fullName" in payload:
        target["fullName"] = full_name
      if "email" in payload:
        target["email"] = email
      if "phone" in payload:
        target["phone"] = phone
      if "mode" in payload and mode:
        target["mode"] = mode
      if "guests" in payload and guests is not None:
        target["guests"] = guests
      if "serviceType" in payload:
        target["serviceType"] = service_type
      if "notes" in payload:
        target["notes"] = notes
      target["updatedAt"] = datetime.now(timezone.utc).isoformat()
      _write_db(data)

    self._send_json({"ok": True, "booking": target})

  def _handle_delete_booking(self, booking_id: str, parsed) -> None:
    query = parse_qs(parsed.query)
    site = _clean_text((query.get("site") or [""])[0], 120)
    if not site:
      self._send_json({"ok": False, "error": "Missing site."}, status=HTTPStatus.BAD_REQUEST)
      return

    with DB_LOCK:
      data = _read_db()
      before_count = len(data.get("bookings", []))
      data["bookings"] = [
        booking
        for booking in data.get("bookings", [])
        if not (_clean_text(booking.get("id"), 180) == booking_id and _clean_text(booking.get("site"), 120) == site)
      ]
      removed = before_count - len(data["bookings"])
      if removed > 0:
        _write_db(data)

    if removed <= 0:
      self._send_json({"ok": False, "error": "Booking not found."}, status=HTTPStatus.NOT_FOUND)
      return
    self._send_json({"ok": True, "removed": removed})

  def _handle_get_staff(self, parsed) -> None:
    query = parse_qs(parsed.query)
    site = _clean_text((query.get("site") or [""])[0], 120)
    if not site:
      self._send_json({"ok": False, "error": "Missing site."}, status=HTTPStatus.BAD_REQUEST)
      return
    with DB_LOCK:
      data = _read_db()
      staff = [member for member in data.get("staffMembers", []) if _clean_text(member.get("site"), 120) == site]
    staff = sorted(staff, key=lambda member: _clean_text(member.get("name"), 120).lower())
    self._send_json({"ok": True, "staffMembers": staff})

  def _handle_post_staff(self) -> None:
    payload = self._read_json_body()
    if payload is None:
      self._send_json({"ok": False, "error": "Invalid JSON payload."}, status=HTTPStatus.BAD_REQUEST)
      return

    site = _clean_text(payload.get("site"), 120)
    name = _clean_text(payload.get("name"), 120)
    password = _clean_text(payload.get("password"), 120)
    hourly_rate = _clean_hourly_rate(payload.get("hourlyRate"), 0.0)
    if not site or not name or not password:
      self._send_json({"ok": False, "error": "site, name and password are required."}, status=HTTPStatus.BAD_REQUEST)
      return

    with DB_LOCK:
      data = _read_db()
      same_site = [member for member in data.get("staffMembers", []) if _clean_text(member.get("site"), 120) == site]
      duplicate = next((member for member in same_site if _clean_text(member.get("name"), 120).lower() == name.lower()), None)
      if duplicate:
        self._send_json({"ok": False, "error": "Staff name already exists for this site."}, status=HTTPStatus.CONFLICT)
        return

      next_staff_id = int(data.get("nextStaffId") or 1)
      now = datetime.now(timezone.utc).isoformat()
      staff = {
        "id": f"{site}-staff-{next_staff_id}",
        "site": site,
        "name": name,
        "password": password,
        "hourlyRate": hourly_rate,
        "createdAt": now,
        "updatedAt": now,
      }
      data.setdefault("staffMembers", []).append(staff)
      data["nextStaffId"] = next_staff_id + 1
      _write_db(data)

    self._send_json({"ok": True, "staffMember": staff}, status=HTTPStatus.CREATED)

  def _handle_put_staff(self, staff_id: str) -> None:
    payload = self._read_json_body()
    if payload is None:
      self._send_json({"ok": False, "error": "Invalid JSON payload."}, status=HTTPStatus.BAD_REQUEST)
      return

    site = _clean_text(payload.get("site"), 120)
    name = _clean_text(payload.get("name"), 120)
    password = _clean_text(payload.get("password"), 120)
    if not site:
      self._send_json({"ok": False, "error": "Missing site."}, status=HTTPStatus.BAD_REQUEST)
      return

    with DB_LOCK:
      data = _read_db()
      staff_members = data.get("staffMembers", [])
      target = next(
        (member for member in staff_members if _clean_text(member.get("id"), 180) == staff_id and _clean_text(member.get("site"), 120) == site),
        None,
      )
      if not target:
        self._send_json({"ok": False, "error": "Staff member not found."}, status=HTTPStatus.NOT_FOUND)
        return

      if name:
        duplicate = next(
          (
            member
            for member in staff_members
            if _clean_text(member.get("site"), 120) == site
            and _clean_text(member.get("id"), 180) != staff_id
            and _clean_text(member.get("name"), 120).lower() == name.lower()
          ),
          None,
        )
        if duplicate:
          self._send_json({"ok": False, "error": "Staff name already exists for this site."}, status=HTTPStatus.CONFLICT)
          return
        target["name"] = name
      if password:
        target["password"] = password
      if "hourlyRate" in payload:
        target["hourlyRate"] = _clean_hourly_rate(payload.get("hourlyRate"), target.get("hourlyRate", 0.0))
      target["updatedAt"] = datetime.now(timezone.utc).isoformat()
      _write_db(data)

    self._send_json({"ok": True, "staffMember": target})

  def _handle_delete_staff(self, staff_id: str, parsed) -> None:
    query = parse_qs(parsed.query)
    site = _clean_text((query.get("site") or [""])[0], 120)
    if not site:
      self._send_json({"ok": False, "error": "Missing site."}, status=HTTPStatus.BAD_REQUEST)
      return

    with DB_LOCK:
      data = _read_db()
      before_staff_count = len(data.get("staffMembers", []))
      before_availability_count = len(data.get("availabilityEntries", []))
      data["staffMembers"] = [
        member
        for member in data.get("staffMembers", [])
        if not (_clean_text(member.get("id"), 180) == staff_id and _clean_text(member.get("site"), 120) == site)
      ]
      data["availabilityEntries"] = [
        entry
        for entry in data.get("availabilityEntries", [])
        if not (_clean_text(entry.get("staffId"), 180) == staff_id and _clean_text(entry.get("site"), 120) == site)
      ]
      removed_staff = before_staff_count - len(data["staffMembers"])
      removed_availability = before_availability_count - len(data["availabilityEntries"])
      _write_db(data)

    if removed_staff <= 0:
      self._send_json({"ok": False, "error": "Staff member not found."}, status=HTTPStatus.NOT_FOUND)
      return
    self._send_json({"ok": True, "removedStaff": removed_staff, "removedAvailability": removed_availability})

  def _handle_staff_login(self) -> None:
    payload = self._read_json_body()
    if payload is None:
      self._send_json({"ok": False, "error": "Invalid JSON payload."}, status=HTTPStatus.BAD_REQUEST)
      return

    site = _clean_text(payload.get("site"), 120)
    name = _clean_text(payload.get("name"), 120)
    password = _clean_text(payload.get("password"), 120)
    if not site or not name or not password:
      self._send_json({"ok": False, "error": "site, name and password are required."}, status=HTTPStatus.BAD_REQUEST)
      return

    with DB_LOCK:
      data = _read_db()
      match = next(
        (
          member
          for member in data.get("staffMembers", [])
          if _clean_text(member.get("site"), 120) == site
          and _clean_text(member.get("name"), 120).lower() == name.lower()
          and _clean_text(member.get("password"), 120) == password
        ),
        None,
      )
    if not match:
      self._send_json({"ok": False, "error": "Invalid name or password."}, status=HTTPStatus.UNAUTHORIZED)
      return

    self._send_json({"ok": True, "staffMember": match})

  def _handle_get_staff_availability(self, parsed) -> None:
    query = parse_qs(parsed.query)
    site = _clean_text((query.get("site") or [""])[0], 120)
    staff_id = _clean_text((query.get("staffId") or [""])[0], 180)
    if not site:
      self._send_json({"ok": False, "error": "Missing site."}, status=HTTPStatus.BAD_REQUEST)
      return

    with DB_LOCK:
      data = _read_db()
      entries = [entry for entry in data.get("availabilityEntries", []) if _clean_text(entry.get("site"), 120) == site]
      if staff_id:
        entries = [entry for entry in entries if _clean_text(entry.get("staffId"), 180) == staff_id]
      staff_by_id = {
        _clean_text(member.get("id"), 180): _clean_text(member.get("name"), 120)
        for member in data.get("staffMembers", [])
        if _clean_text(member.get("site"), 120) == site
      }
    entries = sorted(entries, key=lambda entry: (_clean_text(entry.get("date"), 20), _clean_text(entry.get("startTime"), 20)))
    for entry in entries:
      entry["staffName"] = staff_by_id.get(_clean_text(entry.get("staffId"), 180), "Unknown")
    self._send_json({"ok": True, "availabilityEntries": entries})

  def _handle_post_staff_availability(self) -> None:
    payload = self._read_json_body()
    if payload is None:
      self._send_json({"ok": False, "error": "Invalid JSON payload."}, status=HTTPStatus.BAD_REQUEST)
      return

    site = _clean_text(payload.get("site"), 120)
    actor = _clean_text(payload.get("actor"), 20).lower() or "staff"
    staff_id = _clean_text(payload.get("staffId"), 180)
    availability_date = _clean_text(payload.get("date"), 20)
    start_time = _clean_text(payload.get("startTime"), 10)
    end_time = _clean_text(payload.get("endTime"), 10)
    notes = _clean_text(payload.get("notes"), 500)

    if not site or not staff_id or not availability_date or not start_time or not end_time:
      self._send_json(
        {"ok": False, "error": "site, staffId, date, startTime and endTime are required."},
        status=HTTPStatus.BAD_REQUEST,
      )
      return

    parsed_day = _parse_iso_date(availability_date)
    if not parsed_day:
      self._send_json({"ok": False, "error": "Invalid date format. Use YYYY-MM-DD."}, status=HTTPStatus.BAD_REQUEST)
      return
    if actor == "staff":
      earliest_allowed = date.today() + timedelta(days=7)
      if parsed_day < earliest_allowed:
        self._send_json(
          {"ok": False, "error": f"Availability can only be changed 1 week in advance (from {earliest_allowed.isoformat()})."},
          status=HTTPStatus.BAD_REQUEST,
        )
        return
    if len(start_time) != 5 or len(end_time) != 5 or ":" not in start_time or ":" not in end_time:
      self._send_json({"ok": False, "error": "Invalid time format. Use HH:MM."}, status=HTTPStatus.BAD_REQUEST)
      return
    if end_time <= start_time:
      self._send_json({"ok": False, "error": "endTime must be later than startTime."}, status=HTTPStatus.BAD_REQUEST)
      return

    with DB_LOCK:
      data = _read_db()
      staff_exists = any(
        _clean_text(member.get("id"), 180) == staff_id and _clean_text(member.get("site"), 120) == site
        for member in data.get("staffMembers", [])
      )
      if not staff_exists:
        self._send_json({"ok": False, "error": "Staff member not found."}, status=HTTPStatus.NOT_FOUND)
        return

      entries = data.get("availabilityEntries", [])
      same_day_entry = next(
        (
          entry
          for entry in entries
          if _clean_text(entry.get("site"), 120) == site
          and _clean_text(entry.get("staffId"), 180) == staff_id
          and _clean_text(entry.get("date"), 20) == availability_date
        ),
        None,
      )
      now = datetime.now(timezone.utc).isoformat()
      if same_day_entry:
        if actor == "staff":
          self._send_json(
            {"ok": False, "error": "You already submitted availability for this date. Use change availability."},
            status=HTTPStatus.CONFLICT,
          )
          return
        same_day_entry["startTime"] = start_time
        same_day_entry["endTime"] = end_time
        same_day_entry["notes"] = notes
        same_day_entry["updatedAt"] = now
        saved_entry = same_day_entry
        response_status = HTTPStatus.OK
      else:
        next_entry_id = int(data.get("nextAvailabilityId") or 1)
        saved_entry = {
          "id": f"{site}-availability-{next_entry_id}",
          "site": site,
          "staffId": staff_id,
          "date": availability_date,
          "startTime": start_time,
          "endTime": end_time,
          "notes": notes,
          "createdAt": now,
          "updatedAt": now,
        }
        entries.append(saved_entry)
        data["nextAvailabilityId"] = next_entry_id + 1
        response_status = HTTPStatus.CREATED

      _write_db(data)

    self._send_json({"ok": True, "availabilityEntry": saved_entry}, status=response_status)

  def _handle_put_staff_availability(self, entry_id: str) -> None:
    payload = self._read_json_body()
    if payload is None:
      self._send_json({"ok": False, "error": "Invalid JSON payload."}, status=HTTPStatus.BAD_REQUEST)
      return

    site = _clean_text(payload.get("site"), 120)
    actor = _clean_text(payload.get("actor"), 20).lower() or "admin"
    staff_id = _clean_text(payload.get("staffId"), 180)
    start_time = _clean_text(payload.get("startTime"), 10)
    end_time = _clean_text(payload.get("endTime"), 10)
    notes = _clean_text(payload.get("notes"), 500)
    new_date = _clean_text(payload.get("date"), 20)

    if not site or not start_time or not end_time:
      self._send_json({"ok": False, "error": "site, startTime and endTime are required."}, status=HTTPStatus.BAD_REQUEST)
      return
    if len(start_time) != 5 or len(end_time) != 5 or ":" not in start_time or ":" not in end_time:
      self._send_json({"ok": False, "error": "Invalid time format. Use HH:MM."}, status=HTTPStatus.BAD_REQUEST)
      return
    if end_time <= start_time:
      self._send_json({"ok": False, "error": "endTime must be later than startTime."}, status=HTTPStatus.BAD_REQUEST)
      return

    with DB_LOCK:
      data = _read_db()
      entries = data.get("availabilityEntries", [])
      target = next(
        (
          entry
          for entry in entries
          if _clean_text(entry.get("id"), 180) == entry_id and _clean_text(entry.get("site"), 120) == site
        ),
        None,
      )
      if not target:
        self._send_json({"ok": False, "error": "Availability entry not found."}, status=HTTPStatus.NOT_FOUND)
        return

      target_date = _parse_iso_date(_clean_text(target.get("date"), 20))
      if not target_date:
        self._send_json({"ok": False, "error": "Entry has invalid date."}, status=HTTPStatus.BAD_REQUEST)
        return

      if actor == "staff":
        if not staff_id:
          self._send_json({"ok": False, "error": "staffId is required for staff edits."}, status=HTTPStatus.BAD_REQUEST)
          return
        if _clean_text(target.get("staffId"), 180) != staff_id:
          self._send_json({"ok": False, "error": "You can only edit your own availability."}, status=HTTPStatus.FORBIDDEN)
          return
        if target_date <= date.today():
          self._send_json({"ok": False, "error": "Past/today entries can no longer be changed."}, status=HTTPStatus.BAD_REQUEST)
          return
        week_key = _week_key(target_date)
        weekly_logs = data.get("staffWeeklyChangeLog", [])
        already_changed = any(
          _clean_text(log.get("site"), 120) == site
          and _clean_text(log.get("staffId"), 180) == staff_id
          and _clean_text(log.get("weekKey"), 20) == week_key
          for log in weekly_logs
        )
        if already_changed:
          self._send_json(
            {"ok": False, "error": "You can change availability only once per week."},
            status=HTTPStatus.BAD_REQUEST,
          )
          return
        weekly_logs.append(
          {
            "site": site,
            "staffId": staff_id,
            "weekKey": week_key,
            "entryId": entry_id,
            "changedAt": datetime.now(timezone.utc).isoformat(),
          }
        )
      elif staff_id:
        new_staff_exists = any(
          _clean_text(member.get("id"), 180) == staff_id and _clean_text(member.get("site"), 120) == site
          for member in data.get("staffMembers", [])
        )
        if not new_staff_exists:
          self._send_json({"ok": False, "error": "Selected staff member not found."}, status=HTTPStatus.NOT_FOUND)
          return
        target["staffId"] = staff_id

      if actor != "staff" and new_date:
        parsed_new_date = _parse_iso_date(new_date)
        if not parsed_new_date:
          self._send_json({"ok": False, "error": "Invalid date format. Use YYYY-MM-DD."}, status=HTTPStatus.BAD_REQUEST)
          return
        target["date"] = new_date

      target["startTime"] = start_time
      target["endTime"] = end_time
      target["notes"] = notes
      target["updatedAt"] = datetime.now(timezone.utc).isoformat()
      _write_db(data)

    self._send_json({"ok": True, "availabilityEntry": target})

  def _handle_delete_staff_availability(self, entry_id: str, parsed) -> None:
    query = parse_qs(parsed.query)
    site = _clean_text((query.get("site") or [""])[0], 120)
    if not site:
      self._send_json({"ok": False, "error": "Missing site."}, status=HTTPStatus.BAD_REQUEST)
      return

    with DB_LOCK:
      data = _read_db()
      before_count = len(data.get("availabilityEntries", []))
      data["availabilityEntries"] = [
        entry
        for entry in data.get("availabilityEntries", [])
        if not (_clean_text(entry.get("id"), 180) == entry_id and _clean_text(entry.get("site"), 120) == site)
      ]
      removed = before_count - len(data["availabilityEntries"])
      if removed > 0:
        _write_db(data)

    if removed <= 0:
      self._send_json({"ok": False, "error": "Availability entry not found."}, status=HTTPStatus.NOT_FOUND)
      return
    self._send_json({"ok": True, "removed": removed})

  def _handle_get_staff_schedule(self, parsed) -> None:
    query = parse_qs(parsed.query)
    site = _clean_text((query.get("site") or [""])[0], 120)
    week_start_raw = _clean_text((query.get("weekStart") or [""])[0], 20)
    if not site:
      self._send_json({"ok": False, "error": "Missing site."}, status=HTTPStatus.BAD_REQUEST)
      return

    parsed_week_start = _parse_iso_date(week_start_raw) if week_start_raw else None
    week_start = _start_of_week(parsed_week_start or date.today())
    day_keys = _week_dates(week_start)

    with DB_LOCK:
      data = _read_db()
      staff = [member for member in data.get("staffMembers", []) if _clean_text(member.get("site"), 120) == site]
      availability = [
        entry
        for entry in data.get("availabilityEntries", [])
        if _clean_text(entry.get("site"), 120) == site and _clean_text(entry.get("date"), 20) in day_keys
      ]

    rows = []
    for member in sorted(staff, key=lambda item: _clean_text(item.get("name"), 120).lower()):
      member_id = _clean_text(member.get("id"), 180)
      member_row_days: list[list[dict]] = []
      for day_key in day_keys:
        slots = [
          {
            "id": _clean_text(entry.get("id"), 180),
            "date": _clean_text(entry.get("date"), 20),
            "startTime": _clean_text(entry.get("startTime"), 10),
            "endTime": _clean_text(entry.get("endTime"), 10),
            "notes": _clean_text(entry.get("notes"), 500),
            "staffId": member_id,
            "staffName": _clean_text(member.get("name"), 120),
          }
          for entry in availability
          if _clean_text(entry.get("staffId"), 180) == member_id and _clean_text(entry.get("date"), 20) == day_key
        ]
        slots = sorted(
          [slot for slot in slots if slot.get("startTime") and slot.get("endTime")],
          key=lambda slot: (slot.get("startTime", ""), slot.get("endTime", "")),
        )
        member_row_days.append(slots)
      rows.append({"staffId": member_id, "name": _clean_text(member.get("name"), 120), "days": member_row_days})

    self._send_json({"ok": True, "weekStart": week_start.isoformat(), "days": day_keys, "rows": rows})

  def do_GET(self) -> None:
    parsed = urlparse(self.path)
    route = parsed.path.rstrip("/") or "/"

    if route == "/api/bookings":
      self._handle_get_bookings(parsed)
      return
    if route == "/api/staff":
      self._handle_get_staff(parsed)
      return
    if route == "/api/staff/availability":
      self._handle_get_staff_availability(parsed)
      return
    if route == "/api/staff/schedule":
      self._handle_get_staff_schedule(parsed)
      return

    if route in {"/", "/admin", "/staff"}:
      self._serve_site_page()
      return
    if route in {"/generator", "/editor"}:
      self._serve_generator_page()
      return

    super().do_GET()

  def do_POST(self) -> None:
    parsed = urlparse(self.path)
    route = parsed.path.rstrip("/") or "/"
    if route == "/api/bookings":
      self._handle_post_booking()
      return
    if route == "/api/staff":
      self._handle_post_staff()
      return
    if route == "/api/staff/login":
      self._handle_staff_login()
      return
    if route == "/api/staff/availability":
      self._handle_post_staff_availability()
      return
    self.send_error(HTTPStatus.NOT_FOUND, "Not found")

  def do_PUT(self) -> None:
    parsed = urlparse(self.path)
    route = parsed.path.rstrip("/") or "/"
    parts = [part for part in route.split("/") if part]
    if len(parts) == 3 and parts[0] == "api" and parts[1] == "bookings":
      self._handle_put_booking(parts[2])
      return
    if len(parts) == 4 and parts[0] == "api" and parts[1] == "staff" and parts[2] == "availability":
      self._handle_put_staff_availability(parts[3])
      return
    if len(parts) == 3 and parts[0] == "api" and parts[1] == "staff":
      self._handle_put_staff(parts[2])
      return
    self.send_error(HTTPStatus.NOT_FOUND, "Not found")

  def do_DELETE(self) -> None:
    parsed = urlparse(self.path)
    route = parsed.path.rstrip("/") or "/"
    parts = [part for part in route.split("/") if part]
    if len(parts) == 3 and parts[0] == "api" and parts[1] == "bookings":
      self._handle_delete_booking(parts[2], parsed)
      return
    if len(parts) == 4 and parts[0] == "api" and parts[1] == "staff" and parts[2] == "availability":
      self._handle_delete_staff_availability(parts[3], parsed)
      return
    if len(parts) == 3 and parts[0] == "api" and parts[1] == "staff":
      self._handle_delete_staff(parts[2], parsed)
      return
    self.send_error(HTTPStatus.NOT_FOUND, "Not found")

  def log_message(self, format: str, *args) -> None:
    print(f"[{self.log_date_time_string()}] {format % args}")


def main() -> None:
  parser = argparse.ArgumentParser(description="Local booking + staff API and static site server")
  parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
  parser.add_argument("--port", default=8000, type=int, help="Port to bind (default: 8000)")
  parser.add_argument(
    "--site-file",
    default=None,
    help="Optional HTML file to serve for /, /admin and /staff (relative to project root)",
  )
  args = parser.parse_args()

  def handler(*handler_args, **handler_kwargs):
    return BookingHandler(
      *handler_args,
      directory=str(ROOT_DIR),
      site_file=args.site_file,
      **handler_kwargs,
    )

  server = ThreadingHTTPServer((args.host, args.port), handler)
  site_page = _resolve_site_file(args.site_file)
  site_page_label = site_page.name if site_page else "none"
  print(f"Serving on http://{args.host}:{args.port}")
  print(f"Site page for /, /admin and /staff: {site_page_label}")
  print(f"Database file: {DB_FILE}")
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\nServer stopped.")
  finally:
    server.server_close()


if __name__ == "__main__":
  main()
