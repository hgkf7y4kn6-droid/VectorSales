"""Sign-in for the backend API.

Users are configured on the server with the AUTH_USERS environment variable:
a comma-separated list of `email:password_hash` entries. Generate a hash with

    python auth.py hash-password

Signing in (POST /auth/login) returns a bearer token that the frontend sends
as `Authorization: Bearer <token>` on every other request. Tokens are signed
with AUTH_SECRET (HMAC-SHA256), expire after AUTH_TOKEN_HOURS (default 12),
and stop working if that user's password hash changes or the user is removed.

Managers are listed (by email) in AUTH_MANAGERS, comma-separated. They can
open and edit every account; everyone else sees full details only for the
accounts they own.

With no AUTH_USERS configured, every protected endpoint refuses requests
(fail closed) rather than running open.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from typing import Dict, Optional, Tuple

PBKDF2_ITERATIONS = 600_000
TOKEN_HOURS = float(os.environ.get("AUTH_TOKEN_HOURS", "12"))
MAX_FAILURES_PER_EMAIL = 5
# Higher, because a whole office can share one public IP address.
MAX_FAILURES_PER_IP = 30
FAILURE_WINDOW_SECONDS = 15 * 60


# --- Passwords -----------------------------------------------------------------

def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iterations))
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


# Spent on unknown emails so a login attempt takes the same time either way.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def load_users(raw: Optional[str] = None) -> Dict[str, str]:
    raw = os.environ.get("AUTH_USERS", "") if raw is None else raw
    users = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        email, sep, stored = entry.partition(":")
        if sep and email.strip() and stored.strip():
            users[email.strip().lower()] = stored.strip()
    return users


# --- Tokens --------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _fingerprint(stored_hash: str) -> str:
    # Ties a token to the password it was issued for: changing it signs users out.
    return hashlib.sha256(stored_hash.encode()).hexdigest()[:16]


class Authenticator:
    def __init__(self, users: Dict[str, str], secret: str, managers: Optional[set] = None):
        self.users = users
        self.secret = secret.encode()
        self.managers = {m.strip().lower() for m in (managers or set()) if m.strip()}
        self._failures: Dict[str, list] = {}

    def role(self, email: str) -> str:
        return "manager" if email in self.managers else "rep"

    @property
    def configured(self) -> bool:
        return bool(self.users)

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self.secret, payload.encode(), hashlib.sha256).digest())

    def issue_token(self, email: str, now: Optional[float] = None) -> Tuple[str, int]:
        now = time.time() if now is None else now
        exp = int(now + TOKEN_HOURS * 3600)
        body = {"sub": email, "exp": exp, "fp": _fingerprint(self.users[email])}
        payload = _b64(json.dumps(body, separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}", exp

    def verify_token(self, token: str, now: Optional[float] = None) -> Optional[Dict]:
        now = time.time() if now is None else now
        try:
            payload, signature = token.split(".")
        except (ValueError, AttributeError):
            return None
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            body = json.loads(_unb64(payload))
        except ValueError:
            return None
        stored = self.users.get(body.get("sub", ""))
        if not stored or body.get("fp") != _fingerprint(stored) or body.get("exp", 0) < now:
            return None
        return body

    # --- Integration keys --------------------------------------------------------
    # A per-user key for outside tools (CRM automations, AI note-takers) that can
    # only post notes to /notes/inbound. Derived from the server secret and the
    # user's password hash, so nothing is stored, and it stops working when the
    # password changes or the user is removed.

    def integration_key(self, email: str) -> str:
        stored = self.users[email]
        sig = hmac.new(self.secret, f"integration|{email}|{_fingerprint(stored)}".encode(), hashlib.sha256).digest()
        return f"ik_{_b64(email.encode())}.{_b64(sig)}"

    def verify_integration_key(self, key: str) -> Optional[str]:
        """The user's email if the key is valid, else None."""
        try:
            head, _ = key.split(".")
            email = _unb64(head[3:]).decode() if head.startswith("ik_") else ""
        except (ValueError, TypeError, UnicodeDecodeError):
            return None
        if email not in self.users:
            return None
        return email if hmac.compare_digest(key, self.integration_key(email)) else None

    # --- Login with throttling -------------------------------------------------

    def _recent_failures(self, key: str, now: float) -> list:
        recent = [t for t in self._failures.get(key, []) if now - t < FAILURE_WINDOW_SECONDS]
        self._failures[key] = recent
        return recent

    def locked_out(self, email: str, client_ip: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return (len(self._recent_failures(f"e:{email}", now)) >= MAX_FAILURES_PER_EMAIL
                or len(self._recent_failures(f"ip:{client_ip}", now)) >= MAX_FAILURES_PER_IP)

    def login(self, email: str, password: str, client_ip: str, now: Optional[float] = None) -> Optional[str]:
        """Returns the normalized email on success, None on bad credentials."""
        now = time.time() if now is None else now
        email = (email or "").strip().lower()
        stored = self.users.get(email)
        ok = verify_password(password or "", stored or _DUMMY_HASH) and stored is not None
        if ok:
            self._failures.pop(f"e:{email}", None)
            return email
        for key in (f"e:{email}", f"ip:{client_ip}"):
            self._failures.setdefault(key, []).append(now)
        if len(self._failures) > 10_000:  # keep memory bounded under a flood
            self._failures.clear()
        return None


def from_environment() -> Authenticator:
    secret = os.environ.get("AUTH_SECRET", "")
    if not secret:
        # Works, but every restart/redeploy signs everyone out. Set AUTH_SECRET.
        print("WARNING: AUTH_SECRET is not set; using a random secret for this process.", file=sys.stderr)
        secret = secrets.token_hex(32)
    users = load_users()
    if not users:
        print("WARNING: AUTH_USERS is not set; all API requests will be refused until it is.", file=sys.stderr)
    managers = set(os.environ.get("AUTH_MANAGERS", "").lower().split(","))
    return Authenticator(users, secret, managers)


if __name__ == "__main__":
    if sys.argv[1:] != ["hash-password"]:
        print("usage: python auth.py hash-password")
        sys.exit(2)
    import getpass

    email = input("Email: ").strip().lower()
    password = getpass.getpass("Password (12+ characters): ")
    if len(password) < 12:
        print("Use at least 12 characters.")
        sys.exit(1)
    if password != getpass.getpass("Repeat password: "):
        print("Passwords don't match.")
        sys.exit(1)
    print("\nAdd this entry to AUTH_USERS (comma-separate multiple users):\n")
    print(f"{email}:{hash_password(password)}")
