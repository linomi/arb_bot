"""
Symmetric encryption for exchange credentials at rest (Fernet = AES128-CBC +
HMAC). Key comes from ENCRYPTION_KEY in the environment (.env). Never log or
return decrypted secrets to the frontend -- the credentials router only ever
returns whether a credential is configured, not its value.
"""
import os
from cryptography.fernet import Fernet, InvalidToken

_KEY = os.environ.get("ENCRYPTION_KEY")
_fernet: Fernet | None = None
if _KEY:
    try:
        _fernet = Fernet(_KEY.encode())
    except Exception:
        _fernet = None


class EncryptionNotConfigured(RuntimeError):
    pass


def _require_fernet() -> Fernet:
    if _fernet is None:
        raise EncryptionNotConfigured(
            "ENCRYPTION_KEY is missing/invalid. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "and put it in your .env file."
        )
    return _fernet


def encrypt(plaintext: str) -> str:
    f = _require_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    f = _require_fernet()
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise EncryptionNotConfigured("Stored credential could not be decrypted (wrong/rotated key?).") from e
