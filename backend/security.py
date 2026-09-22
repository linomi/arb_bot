"""
Symmetric encryption for exchange credentials at rest (Fernet).
Key: ENCRYPTION_KEY in environment / .env — loaded lazily so it works even
if dotenv runs slightly after first import.
"""
from __future__ import annotations

import os
from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None
_fernet_loaded = False


class EncryptionNotConfigured(RuntimeError):
    pass


def _load_fernet() -> Fernet | None:
    global _fernet, _fernet_loaded
    if _fernet_loaded and _fernet is not None:
        return _fernet
    key = (os.environ.get("ENCRYPTION_KEY") or "").strip().strip('"').strip("'")
    if not key:
        _fernet = None
        _fernet_loaded = True
        return None
    try:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        _fernet = None
    _fernet_loaded = True
    return _fernet


def reset_fernet_cache() -> None:
    """Test helper / after setting env at runtime."""
    global _fernet, _fernet_loaded
    _fernet = None
    _fernet_loaded = False


def _require_fernet() -> Fernet:
    f = _load_fernet()
    if f is None:
        raise EncryptionNotConfigured(
            "ENCRYPTION_KEY is missing/invalid. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" '
            "and put it in your .env file (project root), then restart the bot."
        )
    return f


def encrypt(plaintext: str) -> str:
    f = _require_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    f = _require_fernet()
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise EncryptionNotConfigured(
            "Stored credential could not be decrypted (wrong/rotated ENCRYPTION_KEY?)."
        ) from e
