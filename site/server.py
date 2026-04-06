"""
PortalX v3 — Enhanced Healthcare Portal Server for Extensive RPA Testing
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Simulates Brightree/MyCGS challenges, now with:
  - Multi-user support (admin, user1, user2) for parallel login tests
  - Prior auth status lookup page (for batch status check scenarios)
  - Persistent ref ID store (lookups after submission)
  - Server-side file upload handling with metadata tracking
  - Per-session audit log (every action recorded)
  - Deterministic mode via query params (?error_rate=0) for reproducible tests
  - Forced popup injection (?popup=alert|error) for deterministic popup tests
  - Slow response mode (?slow=5) for timeout tests
  - Session info endpoint for expiry detection
  - Configurable session TTL via env var

All original v2 features preserved:
  - TOTP-based MFA
  - Concurrent session detection
  - Session expiry mid-flow
  - Random error injection (default rates)
  - API endpoints for wait_for_api_url patterns
"""
import http.server
import json
import os
import random
import socketserver
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", 8888))
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_TTL = int(os.environ.get("SESSION_TTL", 300))  # 5 minutes default

# Upload directory (ephemeral, for testing only)
UPLOAD_DIR = "/tmp/portalx_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── Default error injection rates (can be overridden per-request) ───────────
DEFAULT_RATES = {
    "concurrent_session": 0.05,  # 5% on login
    "dom_error_modal": 0.08,     # 8% on prior_auth page load
    "server_error_post": 0.10,   # 10% on form submit
    "supplier_down": 0.02,       # 2% on API health check
    "alert_on_npi": 0.20,        # 20% on NPI page load
}

# ── Users ────────────────────────────────────────────────────────────────────
USERS = {
    "admin":  {"password": "P@ssw0rd!", "role": "admin",   "display": "Admin User"},
    "user1":  {"password": "Password1!", "role": "agent",  "display": "Agent One"},
    "user2":  {"password": "Password2!", "role": "agent",  "display": "Agent Two"},
    "user3":  {"password": "Password3!", "role": "viewer", "display": "Viewer Three"},
}

# ── Patient data pool (realistic batch data) ────────────────────────────────
PATIENTS = [
    {"id": "P001", "last": "Rivera",   "first": "Alex",     "dob": "1985-06-15", "medicare_id": "1EG4-TE5-MK72"},
    {"id": "P002", "last": "Chen",     "first": "Sarah",    "dob": "1990-03-22", "medicare_id": "2FH5-UF6-NL83"},
    {"id": "P003", "last": "Johnson",  "first": "Marcus",   "dob": "1978-11-08", "medicare_id": "3GI6-VG7-OM94"},
    {"id": "P004", "last": "Patel",    "first": "Priya",    "dob": "1982-07-14", "medicare_id": "4HJ7-WH8-PN15"},
    {"id": "P005", "last": "Williams", "first": "Denise",   "dob": "1995-01-30", "medicare_id": "5IK8-XI9-QO26"},
    {"id": "P006", "last": "Kim",      "first": "David",    "dob": "1988-09-17", "medicare_id": "6JL9-YJ0-RP37"},
    {"id": "P007", "last": "Garcia",   "first": "Maria",    "dob": "1975-12-03", "medicare_id": "7KM0-ZK1-SQ48"},
    {"id": "P008", "last": "Brown",    "first": "James",    "dob": "1992-04-25", "medicare_id": "8LN1-AL2-TR59"},
    {"id": "P009", "last": "Singh",    "first": "Raj",      "dob": "1986-08-11", "medicare_id": "9MO2-BM3-US60"},
    {"id": "P010", "last": "Lee",      "first": "Jennifer", "dob": "1983-02-19", "medicare_id": "0NP3-CN4-VT71"},
]

# ── Session store ────────────────────────────────────────────────────────────
# sid -> { user, logged_in, mfa_done, ..., audit_log: [] }
sessions: dict[str, dict] = {}
sessions_lock = threading.Lock()

# ── Prior auth records (persistent across sessions for status lookups) ───────
# ref_id -> record
prior_auths: dict[str, dict] = {}
prior_auths_lock = threading.Lock()

# Pre-seed some prior auth records so status lookup has data even before any
# workflow runs against this server instance.
STATUSES = ["Pending Review", "Approved", "Denied", "Additional Info Required", "In Review"]
for _ in range(20):
    ref = "PA-" + str(uuid.uuid4())[:8].upper()
    p = random.choice(PATIENTS)
    prior_auths[ref] = {
        "ref_id": ref,
        "patient_id": p["id"],
        "patient_name": f"{p['last']}, {p['first']}",
        "dob": p["dob"],
        "hcpcs": random.choice(["E0601", "E0470", "K0005"]),
        "status": random.choice(STATUSES),
        "submitted_at": time.time() - random.randint(3600, 86400 * 7),
        "submitted_by": random.choice(list(USERS.keys())),
    }


def new_session(user: str = None) -> str:
    sid = str(uuid.uuid4())
    with sessions_lock:
        sessions[sid] = {
            "sid": sid,
            "user": user,
            "logged_in": False,
            "mfa_done": False,
            "terms_done": False,
            "npi": None,
            "last_active": time.time(),
            "created_at": time.time(),
            "audit_log": [],
            "current_patient": None,
            "uploaded_files": [],
        }
    return sid


def get_session(sid: str) -> dict | None:
    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return None
        if time.time() - s["last_active"] > SESSION_TTL:
            del sessions[sid]
            return None
        s["last_active"] = time.time()
        return s


def audit_log(sid: str, action: str, detail: dict = None):
    """Append an audit entry to the session's log."""
    s = sessions.get(sid)
    if not s:
        return
    s["audit_log"].append({
        "timestamp": time.time(),
        "action": action,
        "detail": detail or {},
    })


def get_valid_totp() -> str:
    return str(int(time.time() // 30) % 1000000).zfill(6)


def parse_cookie(cookie_header: str) -> dict:
    cookies = {}
    if not cookie_header:
        return cookies
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def parse_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    body = handler.rfile.read(length).decode()
    return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(body).items()}


def parse_multipart(handler) -> tuple[dict, dict]:
    """
    Parse multipart/form-data. Returns (fields, files) where files maps
    field_name → {"filename", "content_type", "size", "saved_path"}.
    """
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype:
        return {}, {}
    boundary = ctype.split("boundary=")[-1].strip()
    if not boundary:
        return {}, {}
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length)

    fields, files = {}, {}
    # Split on boundary
    sep = ("--" + boundary).encode()
    parts = raw.split(sep)
    for part in parts:
        part = part.strip(b"\r\n-")
        if not part or part == b"--":
            continue
        # Split headers from body
        if b"\r\n\r\n" not in part:
            continue
        header_blob, body = part.split(b"\r\n\r\n", 1)
        headers = {}
        for line in header_blob.decode(errors="replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        if "name=" not in disp:
            continue
        name = disp.split('name="', 1)[1].split('"', 1)[0]
        if "filename=" in disp:
            filename = disp.split('filename="', 1)[1].split('"', 1)[0]
            if filename:
                # Save file
                file_id = str(uuid.uuid4())
                safe_name = filename.replace("/", "_").replace("\\", "_")
                save_path = os.path.join(UPLOAD_DIR, f"{file_id}_{safe_name}")
                body = body.rstrip(b"\r\n")
                with open(save_path, "wb") as f:
                    f.write(body)
                files[name] = {
                    "filename": filename,
                    "content_type": headers.get("content-type", "application/octet-stream"),
                    "size": len(body),
                    "saved_path": save_path,
                    "file_id": file_id,
                }
            else:
                fields[name] = ""
        else:
            fields[name] = body.rstrip(b"\r\n").decode(errors="replace")
    return fields, files


def read_page(name: str) -> bytes:
    path = os.path.join(STATIC_DIR, "pages", name)
    with open(path, "rb") as f:
        return f.read()


def rate(name: str, query: dict) -> float:
    """Get error injection rate — can be overridden via query param."""
    override = query.get(f"rate_{name}", [None])[0]
    if override is not None:
        try:
            return float(override)
        except ValueError:
            pass
    # ?deterministic=1 disables all error injection
    if query.get("deterministic", [None])[0] == "1":
        return 0.0
    return DEFAULT_RATES.get(name, 0.0)


# ── HTTP handler ─────────────────────────────────────────────────────────────

class PortalHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def send_html(self, html: bytes | str, status: int = 200):
        if isinstance(html, str):
            html = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def set_session_cookie(self, sid: str) -> str:
        return f"portal_sid={sid}; Path=/; HttpOnly"

    def get_sid(self) -> str | None:
        cookies = parse_cookie(self.headers.get("Cookie", ""))
        return cookies.get("portal_sid")

    def get_query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def maybe_slow(self):
        """Honor ?slow=N to inject a delay (for timeout testing)."""
        q = self.get_query()
        slow = q.get("slow", [None])[0]
        if slow:
            try:
                time.sleep(float(slow))
            except ValueError:
                pass

    # ── Routing ───────────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        routes = {
            "/": lambda: self.redirect("/login"),
            "/login": self.page_login_get,
            "/mfa": self.page_mfa_get,
            "/terms": self.page_terms_get,
            "/npi": self.page_npi_get,
            "/patient-search": self.page_patient_search_get,
            "/prior-auth": self.page_prior_auth_get,
            "/confirmation": self.page_confirmation_get,
            "/concurrent": self.page_concurrent_get,
            "/session-expired": self.page_session_expired_get,
            "/status": self.page_status_get,
            "/audit": self.page_audit_get,
        }
        if path.startswith("/api/"):
            self.handle_api(path)
        elif path in routes:
            routes[path]()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        routes = {
            "/login": self.page_login_post,
            "/mfa": self.page_mfa_post,
            "/terms": self.page_terms_post,
            "/npi": self.page_npi_post,
            "/patient-search": self.page_patient_search_post,
            "/prior-auth": self.page_prior_auth_post,
            "/status": self.page_status_post,
        }
        if path in routes:
            routes[path]()
        else:
            self.send_response(404)
            self.end_headers()

    # ── API routes ────────────────────────────────────────────────────────────

    def handle_api(self, path: str):
        self.maybe_slow()
        q = self.get_query()

        if path == "/api/supplier-check":
            if random.random() < rate("supplier_down", q):
                self.send_json({"success": False, "message": "Service temporarily unavailable"})
            else:
                self.send_json({"success": True, "status": "operational"})

        elif path == "/api/patient-search":
            time.sleep(0.3)
            # Allow filtering by ?q= param for realistic search
            query_str = q.get("q", [""])[0].lower()
            items = []
            for p in PATIENTS:
                full = f"{p['last']} {p['first']}".lower()
                if not query_str or query_str in full or query_str in p["dob"]:
                    score = 99 if query_str == "" or query_str in full else 72
                    items.append({
                        "id": p["id"],
                        "lastName": p["last"],
                        "firstName": p["first"],
                        "dob": p["dob"],
                        "medicareId": p["medicare_id"],
                        "matchScore": score,
                    })
            if not items:
                # Always return Rivera as a fallback high-score match
                p = PATIENTS[0]
                items = [{
                    "id": p["id"],
                    "lastName": p["last"],
                    "firstName": p["first"],
                    "dob": p["dob"],
                    "medicareId": p["medicare_id"],
                    "matchScore": 99,
                }]
            self.send_json({"items": items, "total": len(items)})

        elif path == "/api/npi-lookup":
            npi = q.get("q", [""])[0]
            results = [
                {"npi": "1234567890", "name": "Dr. Morgan Lee",   "specialty": "Pulmonology"},
                {"npi": "0987654321", "name": "Dr. Casey Torres", "specialty": "Internal Medicine"},
            ]
            matches = [r for r in results if npi in r["npi"] or npi.lower() in r["name"].lower()]
            self.send_json({"results": matches or results})

        elif path == "/api/hcpcs-search":
            query_str = q.get("q", [""])[0].upper()
            codes = [
                {"code": "E0601", "description": "Continuous Positive Airway Pressure (CPAP) Device"},
                {"code": "E0470", "description": "Respiratory Assist Device, BiPAP"},
                {"code": "E0435", "description": "Portable Liquid Oxygen System"},
                {"code": "E1390", "description": "Oxygen Concentrator, Single Delivery Port"},
                {"code": "K0001", "description": "Standard Wheelchair"},
                {"code": "K0005", "description": "Ultralightweight Wheelchair"},
                {"code": "K0010", "description": "Standard-Weight Frame Power Wheelchair"},
            ]
            matches = [c for c in codes if query_str in c["code"] or query_str in c["description"].upper()]
            time.sleep(0.2)
            self.send_json({"results": matches or codes[:3]})

        elif path == "/api/status":
            # Lookup prior auth by ref_id
            ref = q.get("ref", [""])[0].strip().upper()
            with prior_auths_lock:
                rec = prior_auths.get(ref)
            if rec:
                self.send_json({"found": True, "record": rec})
            else:
                self.send_json({"found": False, "message": f"No record found for {ref}"}, 404)

        elif path == "/api/session-info":
            # Used by tests to probe whether the session is still alive
            sid = self.get_sid()
            if not sid:
                self.send_json({"valid": False, "reason": "no_cookie"})
                return
            with sessions_lock:
                s = sessions.get(sid)
                if not s:
                    self.send_json({"valid": False, "reason": "not_found"})
                    return
                elapsed = time.time() - s["last_active"]
                if elapsed > SESSION_TTL:
                    self.send_json({"valid": False, "reason": "expired",
                                    "idle_s": round(elapsed, 1), "ttl": SESSION_TTL})
                else:
                    self.send_json({
                        "valid": True,
                        "user": s.get("user"),
                        "logged_in": s.get("logged_in"),
                        "idle_s": round(elapsed, 1),
                        "ttl": SESSION_TTL,
                        "uploaded_files": len(s.get("uploaded_files", [])),
                        "audit_entries": len(s.get("audit_log", [])),
                    })

        elif path == "/api/audit":
            # Return audit log for current session
            sid = self.get_sid()
            s = get_session(sid) if sid else None
            if not s:
                self.send_json({"error": "no session"}, 401)
                return
            self.send_json({"sid": sid, "user": s.get("user"), "entries": s.get("audit_log", [])})

        elif path == "/api/patients":
            # List all available patients in the test data pool
            self.send_json({"patients": PATIENTS, "total": len(PATIENTS)})

        elif path == "/api/health":
            self.send_json({
                "status": "ok",
                "sessions_active": len(sessions),
                "prior_auths": len(prior_auths),
                "session_ttl": SESSION_TTL,
            })

        else:
            self.send_json({"error": "not found"}, 404)

    # ── Page handlers ─────────────────────────────────────────────────────────

    def page_login_get(self):
        self.send_html(read_page("login.html"))

    def page_login_post(self):
        data = parse_body(self)
        username = data.get("Username", "")
        password = data.get("Password", "")
        q = self.get_query()

        user_rec = USERS.get(username)
        if user_rec and user_rec["password"] == password:
            if random.random() < rate("concurrent_session", q):
                self.redirect("/concurrent")
                return
            sid = new_session(user=username)
            with sessions_lock:
                sessions[sid]["logged_in"] = True
            audit_log(sid, "login", {"user": username, "role": user_rec["role"]})
            self.send_response(302)
            self.send_header("Location", "/mfa")
            self.send_header("Set-Cookie", self.set_session_cookie(sid))
            self.end_headers()
        else:
            html = read_page("login.html").replace(
                b'id="login-error" style="display:none"',
                b'id="login-error" style="display:block"',
            )
            self.send_html(html)

    def page_mfa_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s or not s["logged_in"]:
            self.redirect("/session-expired")
            return
        html = read_page("mfa.html")
        hint = f'<span id="totp-debug" style="display:none">{get_valid_totp()}</span>'.encode()
        html = html.replace(b"<!-- TOTP_HINT -->", hint)
        self.send_html(html)

    def page_mfa_post(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s or not s["logged_in"]:
            self.redirect("/session-expired")
            return
        data = parse_body(self)
        code = data.get("MFACode", "").strip()
        valid = get_valid_totp()
        prior = str((int(time.time() // 30) - 1) % 1000000).zfill(6)
        if code in (valid, prior):
            s["mfa_done"] = True
            audit_log(sid, "mfa_success")
            self.redirect("/terms")
        else:
            audit_log(sid, "mfa_failed", {"code_attempted": code})
            html = read_page("mfa.html")
            hint = f'<span id="totp-debug" style="display:none">{valid}</span>'.encode()
            html = html.replace(b"<!-- TOTP_HINT -->", hint)
            html = html.replace(
                b'id="mfa-error" style="display:none"',
                b'id="mfa-error" style="display:block"',
            )
            self.send_html(html)

    def page_terms_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s or not s.get("mfa_done"):
            self.redirect("/session-expired")
            return
        self.send_html(read_page("terms.html"))

    def page_terms_post(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s:
            self.redirect("/session-expired")
            return
        s["terms_done"] = True
        audit_log(sid, "terms_accepted")
        self.redirect("/npi")

    def page_npi_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s or not s.get("terms_done"):
            self.redirect("/session-expired")
            return
        # Allow forced popup: ?popup=alert
        q = self.get_query()
        forced = q.get("popup", [None])[0]
        inject_alert = forced == "alert" or random.random() < rate("alert_on_npi", q)
        html = read_page("npi_select.html")
        html = html.replace(b"INJECT_ALERT=0", f"INJECT_ALERT={1 if inject_alert else 0}".encode())
        self.send_html(html)

    def page_npi_post(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s:
            self.redirect("/session-expired")
            return
        data = parse_body(self)
        s["npi"] = data.get("npi", "1234567890")
        audit_log(sid, "npi_selected", {"npi": s["npi"]})
        self.redirect("/patient-search")

    def page_patient_search_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s or not s.get("terms_done"):
            self.redirect("/session-expired")
            return
        self.send_html(read_page("patient_search.html"))

    def page_patient_search_post(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s:
            self.redirect("/session-expired")
            return
        q = self.get_query()
        data = parse_body(self)
        s["current_patient"] = {
            "last": data.get("LastName", ""),
            "first": data.get("FirstName", ""),
            "dob": data.get("DateOfBirth", ""),
        }
        audit_log(sid, "patient_search", s["current_patient"])
        if random.random() < rate("server_error_post", q):
            self.redirect("/patient-search?server_error=1")
            return
        self.redirect("/prior-auth")

    def page_prior_auth_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s or not s.get("terms_done"):
            self.redirect("/session-expired")
            return
        q = self.get_query()
        forced = q.get("popup", [None])[0]
        inject_error = forced == "error" or random.random() < rate("dom_error_modal", q)
        html = read_page("prior_auth.html")
        html = html.replace(b"INJECT_ERROR_MODAL=0", f"INJECT_ERROR_MODAL={1 if inject_error else 0}".encode())
        self.send_html(html)

    def page_prior_auth_post(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s:
            self.redirect("/session-expired")
            return
        q = self.get_query()

        # Parse multipart if file upload present
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype:
            fields, files = parse_multipart(self)
            if "documents" in files:
                fmeta = files["documents"]
                s["uploaded_files"].append(fmeta)
                audit_log(sid, "file_uploaded", {
                    "filename": fmeta["filename"],
                    "size": fmeta["size"],
                    "file_id": fmeta["file_id"],
                    "content_type": fmeta["content_type"],
                })
            data = fields
        else:
            data = parse_body(self)

        if random.random() < rate("server_error_post", q):
            audit_log(sid, "submit_failed", {"reason": "server_error_injected"})
            self.redirect("/prior-auth?server_error=1")
            return

        # Create persistent prior auth record
        ref_id = "PA-" + str(uuid.uuid4())[:8].upper()
        s["ref_id"] = ref_id
        patient = s.get("current_patient") or {}
        with prior_auths_lock:
            prior_auths[ref_id] = {
                "ref_id": ref_id,
                "patient_name": f"{patient.get('last', '')}, {patient.get('first', '')}",
                "dob": patient.get("dob", ""),
                "hcpcs": data.get("hcpcs_code", "E0601"),
                "icd10": data.get("icd10_code", ""),
                "status": "Pending Review",
                "submitted_at": time.time(),
                "submitted_by": s.get("user"),
                "uploaded_files": [f["file_id"] for f in s["uploaded_files"]],
                "session_id": sid,
            }
        audit_log(sid, "prior_auth_submitted", {
            "ref_id": ref_id,
            "hcpcs": data.get("hcpcs_code", "E0601"),
            "files_attached": len(s["uploaded_files"]),
        })
        self.redirect("/confirmation")

    def page_confirmation_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s:
            self.redirect("/session-expired")
            return
        ref = s.get("ref_id", "PA-UNKNOWN")
        html = read_page("confirmation.html").replace(b"{{REF_ID}}", ref.encode())
        self.send_html(html)

    def page_concurrent_get(self):
        self.send_html(read_page("concurrent.html"))

    def page_session_expired_get(self):
        self.send_html(read_page("session_expired.html"))

    # ── New pages ─────────────────────────────────────────────────────────────

    def page_status_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s or not s.get("logged_in"):
            self.redirect("/session-expired")
            return
        audit_log(sid, "status_page_view")
        # Optional ?ref= pre-fills the search
        q = self.get_query()
        ref = q.get("ref", [""])[0]
        html = read_page("status.html")
        html = html.replace(b"{{PREFILL_REF}}", ref.encode())
        self.send_html(html)

    def page_status_post(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s:
            self.redirect("/session-expired")
            return
        data = parse_body(self)
        ref = data.get("ref_number", "").strip().upper()
        audit_log(sid, "status_lookup", {"ref": ref})
        self.redirect(f"/status?ref={ref}")

    def page_audit_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s:
            self.redirect("/session-expired")
            return
        html = read_page("audit.html").replace(b"{{SID}}", sid.encode())
        self.send_html(html)


# ── Threaded TCP server for concurrent connections ──────────────────────────
class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with ThreadedTCPServer(("", PORT), PortalHandler) as httpd:
        print(f"PortalX v3 running on port {PORT} (threaded)")
        print(f"Session TTL: {SESSION_TTL}s")
        print(f"Users: {list(USERS.keys())}")
        print(f"Pre-seeded prior auths: {len(prior_auths)}")
        httpd.serve_forever()
