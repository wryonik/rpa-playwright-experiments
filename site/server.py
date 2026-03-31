"""
PortalX v2 — Realistic Healthcare Portal Server
Simulates the specific challenges found in Brightree and MyCGS:
  - Multi-page flow with server-side session state
  - TOTP-based MFA (30-second rotating code)
  - Concurrent session detection (5% on login)
  - Session expiry mid-flow (after 5 min inactivity)
  - Jurisdiction/Terms acceptance page
  - NPI selection
  - Patient search with match-score table
  - "An Error occurred" modal (8% on navigations)
  - JS alert "Server Error" (10% on form submits)
  - API endpoints for automation wait_for_api_url patterns
  - Vuetify v-select and quick-lookup field markup
"""
import http.server
import json
import math
import os
import random
import socketserver
import time
import uuid
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", 8888))
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Session store ─────────────────────────────────────────────────────────────
# sid -> { logged_in, mfa_done, terms_done, npi, last_active }
sessions: dict[str, dict] = {}
SESSION_TTL = 300  # 5 minutes of inactivity → session expires

def new_session() -> str:
    sid = str(uuid.uuid4())
    sessions[sid] = {
        "logged_in": False,
        "mfa_done": False,
        "terms_done": False,
        "npi": None,
        "last_active": time.time(),
    }
    return sid

def get_session(sid: str) -> dict | None:
    s = sessions.get(sid)
    if not s:
        return None
    if time.time() - s["last_active"] > SESSION_TTL:
        del sessions[sid]
        return None
    s["last_active"] = time.time()
    return s

def get_valid_totp() -> str:
    """Returns the current valid 6-digit TOTP code (changes every 30s)."""
    t = int(time.time() // 30)
    return str(t % 1000000).zfill(6)

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
    return {k: v[0] if len(v) == 1 else v
            for k, v in parse_qs(body).items()}

def read_page(name: str) -> bytes:
    path = os.path.join(STATIC_DIR, "pages", name)
    with open(path, "rb") as f:
        return f.read()


class PortalHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default logging

    def send_html(self, html: bytes | str, status: int = 200):
        if isinstance(html, str):
            html = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
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
        }
        if path in routes:
            routes[path]()
        else:
            self.send_response(404)
            self.end_headers()

    # ── API routes ────────────────────────────────────────────────────────────

    def handle_api(self, path: str):
        """Simulates the API endpoints automations wait for."""
        if path == "/api/supplier-check":
            # 2% chance site is "down" — mirrors MyCGS GetSupplierDetails
            if random.random() < 0.02:
                self.send_json({"success": False, "message": "Service temporarily unavailable"})
            else:
                self.send_json({"success": True, "status": "operational"})

        elif path == "/api/patient-search":
            # Simulates Brightree PatientIntakes/SearchJson — used for wait_for_api_url
            time.sleep(0.3)  # simulate processing
            self.send_json({
                "items": [
                    {"id": "P001", "lastName": "Rivera",  "firstName": "Alex",    "dob": "1985-06-15", "matchScore": 99},
                    {"id": "P002", "lastName": "Rivera",  "firstName": "A.",      "dob": "1985-06-15", "matchScore": 72},
                    {"id": "P003", "lastName": "Riveran", "firstName": "Alexis",  "dob": "1986-01-20", "matchScore": 41},
                ],
                "total": 3,
            })

        elif path == "/api/npi-lookup":
            qs = parse_qs(urlparse(self.path).query)
            npi = qs.get("q", [""])[0]
            results = [
                {"npi": "1234567890", "name": "Dr. Morgan Lee",   "specialty": "Pulmonology"},
                {"npi": "0987654321", "name": "Dr. Casey Torres", "specialty": "Internal Medicine"},
            ]
            matches = [r for r in results if npi in r["npi"] or npi.lower() in r["name"].lower()]
            self.send_json({"results": matches or results})

        elif path == "/api/hcpcs-search":
            qs = parse_qs(urlparse(self.path).query)
            q = qs.get("q", [""])[0].upper()
            codes = [
                {"code": "E0601", "description": "Continuous Positive Airway Pressure (CPAP) Device"},
                {"code": "E0470", "description": "Respiratory Assist Device, BiPAP"},
                {"code": "E0435", "description": "Portable Liquid Oxygen System"},
                {"code": "E1390", "description": "Oxygen Concentrator, Single Delivery Port"},
                {"code": "K0001", "description": "Standard Wheelchair"},
                {"code": "K0005", "description": "Ultralightweight Wheelchair"},
                {"code": "K0010", "description": "Standard-Weight Frame Power Wheelchair"},
            ]
            matches = [c for c in codes if q in c["code"] or q in c["description"].upper()]
            time.sleep(0.2)
            self.send_json({"results": matches or codes[:3]})
        else:
            self.send_json({"error": "not found"}, 404)

    # ── Page handlers ─────────────────────────────────────────────────────────

    def page_login_get(self):
        self.send_html(read_page("login.html"))

    def page_login_post(self):
        data = parse_body(self)
        username = data.get("Username", "")
        password = data.get("Password", "")

        if username == "admin" and password == "P@ssw0rd!":
            # 5% chance: concurrent session detected (mirrors MyCGS behavior)
            if random.random() < 0.05:
                self.redirect("/concurrent")
                return
            sid = new_session()
            sessions[sid]["logged_in"] = True
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
        # Embed the valid TOTP code hint for debugging (hidden element)
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
        # Allow current window and one prior (handles edge cases)
        prior = str((int(time.time() // 30) - 1) % 1000000).zfill(6)
        if code in (valid, prior):
            s["mfa_done"] = True
            self.redirect("/terms")
        else:
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
        self.redirect("/npi")

    def page_npi_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s or not s.get("terms_done"):
            self.redirect("/session-expired")
            return
        self.send_html(read_page("npi_select.html"))

    def page_npi_post(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s:
            self.redirect("/session-expired")
            return
        data = parse_body(self)
        s["npi"] = data.get("npi", "1234567890")
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
        # 10% chance: server error JS alert injected via redirect param
        if random.random() < 0.10:
            self.redirect("/patient-search?server_error=1")
            return
        self.redirect("/prior-auth")

    def page_prior_auth_get(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s or not s.get("terms_done"):
            self.redirect("/session-expired")
            return
        # 8% chance: "An Error occurred" modal (mirrors MyCGS behavior)
        inject_error = "1" if random.random() < 0.08 else "0"
        html = read_page("prior_auth.html")
        html = html.replace(b"INJECT_ERROR_MODAL=0", f"INJECT_ERROR_MODAL={inject_error}".encode())
        self.send_html(html)

    def page_prior_auth_post(self):
        sid = self.get_sid()
        s = get_session(sid) if sid else None
        if not s:
            self.redirect("/session-expired")
            return
        # 10% chance: server error dialog
        if random.random() < 0.10:
            self.redirect("/prior-auth?server_error=1")
            return
        s["ref_id"] = "PA-" + str(uuid.uuid4())[:8].upper()
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


with socketserver.TCPServer(("", PORT), PortalHandler) as httpd:
    httpd.allow_reuse_address = True
    print(f"PortalX v2 running on port {PORT}")
    httpd.serve_forever()
