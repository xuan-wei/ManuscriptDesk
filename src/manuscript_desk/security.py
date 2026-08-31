import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
RESERVED_USERNAMES = {"api", "auth", "login", "logout", "register", "static", "system", "root"}
LEGACY_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


@dataclass(frozen=True)
class PasswordCheck:
    valid: bool
    upgraded_hash: str | None = None


def normalize_email(value: str) -> str:
    email = str(value or "").strip().casefold()
    if not email or len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValueError("invalid email address")
    return email


def normalize_username(value: str) -> str:
    username = str(value or "").strip().casefold()
    if not USERNAME_RE.fullmatch(username) or username in RESERVED_USERNAMES:
        raise ValueError("invalid username")
    return username


def hash_password(password: str) -> str:
    if (
        len(password) < 8
        or len(password) > 1024
        or not any(character.isalpha() for character in password)
        or not any(character.isdigit() for character in password)
    ):
        raise ValueError(
            "password must contain between 8 and 1024 characters, including at least one letter and one number"
        )
    return _password_hasher.hash(password)


def verify_password(password: str, stored_hash: str, scheme: str) -> PasswordCheck:
    if scheme == "legacy_sha256":
        if not LEGACY_SHA256_RE.fullmatch(stored_hash or ""):
            return PasswordCheck(False)
        candidate = hashlib.sha256(password.encode("utf-8")).hexdigest()
        valid = hmac.compare_digest(candidate, stored_hash)
        # Existing legacy passwords may predate the current password policy.
        # A successful legacy login must still upgrade the exact password to
        # Argon2id; the policy applies only when choosing a new password.
        upgraded = _password_hasher.hash(password) if valid else None
        return PasswordCheck(valid, upgraded)
    if scheme != "argon2id":
        return PasswordCheck(False)
    try:
        valid = _password_hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return PasswordCheck(False)
    upgraded = _password_hasher.hash(password) if _password_hasher.check_needs_rehash(stored_hash) else None
    return PasswordCheck(valid, upgraded)


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token(session_token: str, csrf_secret: str) -> str:
    return hmac.new(csrf_secret.encode("utf-8"), session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf(session_token: str, csrf_secret: str, presented: str) -> bool:
    return hmac.compare_digest(csrf_token(session_token, csrf_secret), str(presented or ""))


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def otp_hmac(key: bytes, email: str, purpose: str, code: str) -> str:
    message = "\0".join((normalize_email(email), purpose, code)).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_otp(key: bytes, email: str, purpose: str, code: str, stored_hmac: str) -> bool:
    return hmac.compare_digest(otp_hmac(key, email, purpose, code), stored_hmac)
