import base64
import hashlib
import hmac
import secrets

try:
    from passlib.context import CryptContext
except Exception:  # pragma: no cover
    CryptContext = None

try:
    import bcrypt
except Exception:  # pragma: no cover
    bcrypt = None


PBKDF2_SCHEME = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000

_legacy_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto") if CryptContext else None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_SCHEME}${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def _verify_pbkdf2(password: str, hashed: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = hashed.split("$", 3)
        if scheme != PBKDF2_SCHEME:
            return False
        rounds = int(iterations)
        salt = _b64decode(salt_b64)
        expected = _b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False

    if hashed.startswith(f"{PBKDF2_SCHEME}$"):
        return _verify_pbkdf2(password, hashed)

    # Direct bcrypt hashes ($2a$, $2b$, $2y$).
    if bcrypt and hashed.startswith("$2"):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            pass

    # Backward compatibility for older hashes generated with passlib.
    if _legacy_context:
        try:
            return _legacy_context.verify(password, hashed)
        except Exception:
            return False
    return False
