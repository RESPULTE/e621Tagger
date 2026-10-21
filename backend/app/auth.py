"""Single administrator authentication; only session token hashes reach SQLite."""

import asyncio
import getpass
import hashlib
import hmac
import secrets
import sys
import time

from fastapi import HTTPException, Request


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return algorithm == "scrypt" and hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def require_session(request: Request):
    token = request.cookies.get(request.app.state.settings.session_cookie_name, "")
    row = await request.app.state.db.one(
        "SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash(token), time.time())
    )
    if not row:
        raise HTTPException(401, "Sign in to continue")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf_token"]):
            raise HTTPException(403, "Invalid CSRF token")
    return row


async def login(request: Request, password: str):
    db, settings = request.app.state.db, request.app.state.settings
    now = time.time()
    # Global limit also works behind a proxy without trusting forwarded IP headers.
    async with db.transaction() as conn:
        row = await (await conn.execute("SELECT * FROM login_attempts WHERE key='admin'")).fetchone()
        attempts = row["attempts"] if row and row["reset_at"] > now else 0
        reset_at = row["reset_at"] if row and row["reset_at"] > now else now + 60
        if attempts >= 10:
            raise HTTPException(429, "Too many sign-in attempts; try again in a minute", headers={"Retry-After": "60"})
        await conn.execute("INSERT OR REPLACE INTO login_attempts VALUES('admin',?,?)", (attempts + 1, reset_at))
    if not await asyncio.to_thread(verify_password, password, settings.password_hash):
        raise HTTPException(401, "Incorrect password")
    return await new_session(request)


async def new_session(request: Request):
    db, settings = request.app.state.db, request.app.state.settings
    now = time.time()
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        await conn.execute(
            "INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), csrf, now + settings.session_hours * 3600)
        )
        await conn.execute("DELETE FROM login_attempts WHERE key='admin'")
    return token, csrf


if __name__ == "__main__":
    if sys.argv[1:] != ["hash-password"]:
        raise SystemExit("Usage: python -m backend.app.auth hash-password")
    password = getpass.getpass("Studio password: ")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords did not match")
    print(hash_password(password))
