"""Authentication for the web API — stdlib crypto only (no extra deps).

Two credential types are accepted, both via the same dependency:

- **Bearer API token** — a value from [web] api_tokens, sent as
  ``Authorization: Bearer <token>``. Intended for scripts/other apps.
- **Session token** — an HMAC-signed token minted by POST /api/login after
  an admin username/password check. Sent either as ``Authorization: Bearer
  <token>`` or in the ``mcbot_session`` cookie (for the browser UI).

Passwords are stored as PBKDF2-HMAC-SHA256 hashes
(``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``); generate one with
``./mcbot.py --hash-password``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

_PBKDF2_ITERATIONS = 200_000
_SESSION_TTL = 12 * 3600  # 12 hours


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------
def hash_password(password: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters_s, salt_hex, hash_hex = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(dk, expected)


# --------------------------------------------------------------------------
# Session tokens (compact HMAC-signed, no server-side store)
# --------------------------------------------------------------------------
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Auth:
    """Holds the web auth config and validates credentials per request."""

    SESSION_COOKIE = "mcbot_session"
    session_ttl = _SESSION_TTL

    def __init__(self, cfg):
        self.cfg = cfg
        self._secret = (cfg.web_session_secret or "").encode("utf-8")

    # ----- session tokens
    def make_session(self, username: str, ttl: int = _SESSION_TTL) -> str:
        payload = {"u": username, "exp": int(time.time()) + ttl}
        body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(self._secret, body.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64e(sig)}"

    def verify_session(self, token: str) -> Optional[str]:
        if not self._secret or not token or "." not in token:
            return None
        body, sig_s = token.rsplit(".", 1)
        expected = hmac.new(
            self._secret, body.encode(), hashlib.sha256
        ).digest()
        try:
            if not hmac.compare_digest(expected, _b64d(sig_s)):
                return None
            payload = json.loads(_b64d(body))
        except Exception:
            return None
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload.get("u")

    # ----- login
    def verify_login(self, username: str, password: str) -> bool:
        if not self.cfg.web_admin_user or not self.cfg.web_admin_password_hash:
            return False
        if not hmac.compare_digest(username, self.cfg.web_admin_user):
            return False
        return verify_password(password, self.cfg.web_admin_password_hash)

    # ----- api tokens
    def _match_api_token(self, token: str) -> bool:
        for t in self.cfg.web_api_tokens:
            if hmac.compare_digest(token, t):
                return True
        return False

    # ----- request identification
    def identify_token_value(self, token: str) -> Optional[str]:
        """Map a raw token (API token or session token) to an identity."""
        if not token:
            return None
        if self._match_api_token(token):
            return f"token:{token[:6]}…"
        user = self.verify_session(token)
        if user:
            return f"user:{user}"
        return None

    def identify(self, request) -> Optional[str]:
        """Return an actor identity string for an HTTP request, or None if
        unauthenticated. Used both as the access gate and the audit actor."""
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            ident = self.identify_token_value(auth_header[7:].strip())
            if ident:
                return ident
        cookie = request.cookies.get(self.SESSION_COOKIE)
        if cookie:
            user = self.verify_session(cookie)
            if user:
                return f"user:{user}"
        return None

    def identify_ws(self, websocket) -> Optional[str]:
        """Like identify() but for a WebSocket: also accepts a ?token=
        query parameter (browsers can't set Authorization on a WS)."""
        token = websocket.query_params.get("token")
        if token:
            ident = self.identify_token_value(token)
            if ident:
                return ident
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            ident = self.identify_token_value(auth_header[7:].strip())
            if ident:
                return ident
        cookie = websocket.cookies.get(self.SESSION_COOKIE)
        if cookie:
            user = self.verify_session(cookie)
            if user:
                return f"user:{user}"
        return None
