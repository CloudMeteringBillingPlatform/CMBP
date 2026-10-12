"""Passwords, API keys and sessions (spec section 13)."""

import hashlib
import hmac
import json
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import settings
from app.redis_client import get_redis

# argon2id with a small memory cost (19 MiB, the OWASP minimum) so a burst of logins
# cannot exhaust a 192 MB container.
_hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)

API_KEY_PREFIX = "mk"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, display_prefix, key_hash). Only the hash and prefix are stored."""
    public = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full_key = f"{API_KEY_PREFIX}_{public}_{secret}"
    return full_key, f"{API_KEY_PREFIX}_{public}", sha256_hex(full_key)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ------------------------------------------------------------------ sessions (server-side, Redis)
SESSION_COOKIE = "session"


def _session_key(token: str) -> str:
    # Store a hash of the token so a read of Redis does not reveal usable session tokens.
    return f"session:{sha256_hex(token)}"


async def create_session(user_id: str) -> str:
    token = generate_token()
    await get_redis().set(
        _session_key(token), json.dumps({"user_id": user_id}), ex=settings.session_ttl_seconds
    )
    return token


async def read_session(token: str) -> dict | None:
    raw = await get_redis().get(_session_key(token))
    return json.loads(raw) if raw else None


async def delete_session(token: str) -> None:
    await get_redis().delete(_session_key(token))
