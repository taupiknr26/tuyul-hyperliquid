"""Dashboard accounts, sign-in sessions and roles.

Three roles, each including the one before it:

``viewer``    read everything the dashboard shows
``operator``  also pause, resume, kill, close and the other commands
``admin``     also change the appearance settings and manage accounts

The per-process control token (``data/control.token``, owner-only) keeps
working for ``botctl`` on the bot host and counts as ``admin``: whoever
can read that file already controls the process.

Nothing here can reach a key, a withdrawal or the exchange: a role only
decides which of the existing, backend-validated commands a browser may
send.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from hlbot.db.database import Database
from hlbot.timeutil import Clock

ROLES = ("viewer", "operator", "admin")
_RANK = {role: rank for rank, role in enumerate(ROLES)}

SESSION_HOURS = 12
USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")
PASSWORD_MIN, PASSWORD_MAX = 10, 256

# scrypt: 32 MiB and roughly 0.1 s per guess on current hardware.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2**15, 8, 1, 32
_SCRYPT_MAXMEM = 128 * 1024 * 1024

# Sign-in throttling: failures inside the window before further attempts are refused.
THROTTLE_WINDOW_MS = 15 * 60 * 1000
THROTTLE_PER_USER = 5
THROTTLE_PER_ADDRESS = 20


class AuthError(Exception):
    """A refused request, with the HTTP status that describes it."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    via: str  # "session" (a signed-in browser) or "token" (the local control token)

    def allows(self, role: str) -> bool:
        return _RANK[self.role] >= _RANK[role]


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN, maxmem=_SCRYPT_MAXMEM
    )
    encode = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${encode(salt)}${encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_text, digest_text = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_text), base64.b64decode(digest_text)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=_SCRYPT_MAXMEM
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# Verified against when the user does not exist, so an unknown name takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_username(name: Any) -> str:
    text = str(name or "").strip().lower()
    if not USERNAME_RE.fullmatch(text):
        raise AuthError("username must be 3 to 32 characters: a-z, 0-9, dot, dash or underscore, starting with a letter or digit")
    return text


def check_password(password: Any) -> str:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise AuthError(f"password must be {PASSWORD_MIN} to {PASSWORD_MAX} characters")
    return password


def check_role(role: Any) -> str:
    if role not in ROLES:
        raise AuthError(f"role must be one of {', '.join(ROLES)}")
    return str(role)


class Auth:
    def __init__(self, db: Database, clock: Optional[Clock] = None, session_hours: float = SESSION_HOURS):
        self.db = db
        self.clock = clock or Clock()
        self.session_ms = int(session_hours * 3600 * 1000)
        self._failures: dict[str, deque[int]] = {}
        self._hashing = asyncio.Semaphore(2)  # each hash holds 32 MiB; never let sign-ins pile up memory

    async def _hash(self, password: str) -> str:
        async with self._hashing:
            return await asyncio.to_thread(hash_password, password)  # off the event loop: the trading loop shares it

    async def _verify(self, password: str, stored: str) -> bool:
        async with self._hashing:
            return await asyncio.to_thread(verify_password, password, stored)

    # ------------------------------------------------------------ accounts
    def user_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM users", default=0))

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT username, role, disabled, created_at, last_login_at FROM users ORDER BY username")
        return [{**row, "disabled": bool(row["disabled"])} for row in rows]

    def _user(self, name: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM users WHERE username = ?", (str(name or "").strip().lower(),))
        if row is None:
            raise AuthError("no such user", 404)
        return row

    def _other_active_admins(self, user_id: int) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?", (user_id,), default=0))

    async def create_user(self, name: Any, password: Any, role: Any, by: str) -> dict[str, Any]:
        username, role = check_username(name), check_role(role)
        hashed = await self._hash(check_password(password))
        if self.db.one("SELECT 1 FROM users WHERE username = ?", (username,)):
            raise AuthError("that username is taken", 409)
        now = self.clock.iso()
        with self.db.tx():
            self.db.execute(
                "INSERT INTO users (username, password_hash, role, disabled, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
                (username, hashed, role, now, now),
            )
            self.db.audit("user.created", {"user": username, "role": role, "by": by})
        return {"username": username, "role": role}

    async def set_password(self, name: str, password: Any, by: str, keep_token: Optional[str] = None) -> None:
        """Change a password and sign that account out everywhere, except
        the session that made the change (``keep_token``)."""
        hashed = await self._hash(check_password(password))
        user = self._user(name)
        with self.db.tx():
            self.db.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (hashed, self.clock.iso(), user["id"]))
            self.db.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user["id"], _token_hash(keep_token) if keep_token else "")
            )
            self.db.audit("user.password_changed", {"user": user["username"], "by": by})

    async def change_own_password(self, name: str, current: Any, new: Any, token: str) -> None:
        user = self._user(name)
        if not isinstance(current, str) or not await self._verify(current, user["password_hash"]):
            raise AuthError("the current password is wrong", 403)
        await self.set_password(name, new, by=user["username"], keep_token=token)

    def update_user(self, name: str, by: str, *, role: Any = None, disabled: Any = None) -> dict[str, Any]:
        user = self._user(name)
        new_role = check_role(role) if role is not None else user["role"]
        new_disabled = int(bool(disabled)) if disabled is not None else user["disabled"]
        loses_admin = user["role"] == "admin" and not user["disabled"] and (new_role != "admin" or new_disabled)
        if loses_admin and not self._other_active_admins(user["id"]):
            raise AuthError("this is the last active admin; make another admin first", 409)
        with self.db.tx():
            self.db.execute(
                "UPDATE users SET role = ?, disabled = ?, updated_at = ? WHERE id = ?", (new_role, new_disabled, self.clock.iso(), user["id"])
            )
            if new_disabled:
                self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            self.db.audit("user.updated", {"user": user["username"], "role": new_role, "disabled": bool(new_disabled), "by": by})
        return {"username": user["username"], "role": new_role, "disabled": bool(new_disabled)}

    def delete_user(self, name: str, by: str) -> None:
        user = self._user(name)
        if user["role"] == "admin" and not user["disabled"] and not self._other_active_admins(user["id"]):
            raise AuthError("this is the last active admin; make another admin first", 409)
        with self.db.tx():
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            self.db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
            self.db.audit("user.deleted", {"user": user["username"], "by": by})

    # ------------------------------------------------------------ sign-in throttling
    def _recent(self, key: str) -> deque[int]:
        failures = self._failures.setdefault(key, deque())
        cutoff = self.clock.now_ms() - THROTTLE_WINDOW_MS
        while failures and failures[0] <= cutoff:
            failures.popleft()
        return failures

    def _check_throttle(self, username: str, address: str) -> None:
        for key, limit in ((f"user:{username}", THROTTLE_PER_USER), (f"addr:{address}", THROTTLE_PER_ADDRESS)):
            failures = self._recent(key)
            if len(failures) >= limit:
                wait_min = max(1, round((failures[0] + THROTTLE_WINDOW_MS - self.clock.now_ms()) / 60000))
                raise AuthError(f"too many failed sign-ins; try again in about {wait_min} min", 429)

    def _record_failure(self, username: str, address: str) -> None:
        if len(self._failures) > 2000:  # bounded: drop the keys that have aged out
            for key in [k for k in self._failures if not self._recent(k)]:
                del self._failures[key]
        for key in (f"user:{username}", f"addr:{address}"):
            self._recent(key).append(self.clock.now_ms())

    # ------------------------------------------------------------ sessions
    async def login(self, name: Any, password: Any, address: str = "") -> tuple[str, Principal]:
        """Check a name and password. Returns the new session token (shown
        to the browser once, stored only as a hash) and who signed in."""
        username = str(name or "").strip().lower()[:64]
        self._check_throttle(username, address)
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        candidate = password if isinstance(password, str) and len(password) <= PASSWORD_MAX else ""
        matches = await self._verify(candidate, row["password_hash"] if row else _DUMMY_HASH)
        if row is None or not matches or row["disabled"]:
            self._record_failure(username, address)
            self.db.audit("auth.login_failed", {"user": username, "address": address})
            raise AuthError("wrong username or password", 401)  # the same answer for every cause
        self._failures.pop(f"user:{username}", None)
        return self._open_session(row), Principal(row["username"], row["role"], "session")

    def _open_session(self, user: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(32)
        now_ms = self.clock.now_ms()
        with self.db.tx():
            self.db.execute("DELETE FROM sessions WHERE expires_ms <= ?", (now_ms,))
            self.db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_ms) VALUES (?, ?, ?, ?)",
                (_token_hash(token), user["id"], self.clock.iso(), now_ms + self.session_ms),
            )
            self.db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (self.clock.iso(), user["id"]))
            self.db.audit("auth.login", {"user": user["username"], "role": user["role"]})
        return token

    async def setup_first_admin(self, name: Any, password: Any) -> tuple[str, Principal]:
        """Create the first account. Only possible while there are none."""
        if self.user_count():
            raise AuthError("setup is already done; sign in instead", 409)
        created = await self.create_user(name, password, "admin", by="setup")
        return self._open_session(self._user(created["username"])), Principal(created["username"], "admin", "session")

    def session(self, token: str) -> Optional[Principal]:
        """Who a session token belongs to. The role is read on every
        request, so a role change or a disabled account applies at once."""
        if not token:
            return None
        row = self.db.one(
            "SELECT u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_ms > ? AND u.disabled = 0",
            (_token_hash(token), self.clock.now_ms()),
        )
        return Principal(row["username"], row["role"], "session") if row else None

    def logout(self, token: str) -> None:
        if not token:
            return
        row = self.db.one("SELECT u.username FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (_token_hash(token),))
        with self.db.tx():
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
            if row:
                self.db.audit("auth.logout", {"user": row["username"]})
