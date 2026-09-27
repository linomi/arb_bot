from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models import Credential
from backend.schemas import CredentialIn
from backend import security

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

SUPPORTED_EXCHANGES = ("nobitex", "xt")


def _normalize_exchange(exchange: str | None) -> str:
    ex = (exchange or "nobitex").strip().lower()
    if ex not in SUPPORTED_EXCHANGES:
        raise HTTPException(400, f"exchange must be one of {SUPPORTED_EXCHANGES}")
    return ex


@router.get("/status")
def credential_status(
    exchange: str = Query("nobitex"),
    db: Session = Depends(get_db),
):
    ex = _normalize_exchange(exchange)
    cred = db.query(Credential).filter_by(exchange=ex).first()
    if not cred:
        return {"configured": False, "auth_method": None, "exchange": ex}
    configured = bool(
        cred.encrypted_token
        or (cred.encrypted_api_key and cred.encrypted_api_secret)
    )
    return {"configured": configured, "auth_method": cred.auth_method, "exchange": ex}


@router.post("")
def save_credentials(body: CredentialIn, db: Session = Depends(get_db)):
    try:
        ex = _normalize_exchange(body.exchange)
        cred = db.query(Credential).filter_by(exchange=ex).first()
        if cred is None:
            cred = Credential(exchange=ex)
            db.add(cred)

        method = (body.auth_method or "token").strip().lower()

        if method == "token":
            if ex != "nobitex":
                raise HTTPException(400, "auth_method=token is only supported for nobitex")
            if not body.token:
                raise HTTPException(400, "token is required for auth_method=token")
            cred.auth_method = "token"
            cred.encrypted_token = security.encrypt(body.token)
            cred.encrypted_api_key = None
            cred.encrypted_api_secret = None

        elif method == "key_signature":
            if ex != "nobitex":
                raise HTTPException(
                    400, "auth_method=key_signature is only supported for nobitex"
                )
            if not body.api_key or not body.api_secret_pem:
                raise HTTPException(
                    400,
                    "api_key and api_secret_pem are required for auth_method=key_signature",
                )
            # Nobitex Ed25519 — basic non-empty validation; full parse happens in client
            secret = body.api_secret_pem.strip()
            if len(secret) < 32:
                raise HTTPException(400, "api_secret_pem looks too short for Ed25519")
            cred.auth_method = "key_signature"
            cred.encrypted_api_key = security.encrypt(body.api_key)
            cred.encrypted_api_secret = security.encrypt(body.api_secret_pem)
            cred.encrypted_token = None

        elif method == "key_secret":
            if ex != "xt":
                raise HTTPException(
                    400, "auth_method=key_secret is only supported for xt"
                )
            if not body.api_key or not body.api_secret_pem:
                raise HTTPException(
                    400,
                    "api_key and api_secret_pem (HMAC secret) are required for auth_method=key_secret",
                )
            # XT uses plain HMAC apiKey+secret — no PEM format required
            if "BEGIN" in body.api_secret_pem and "PRIVATE KEY" in body.api_secret_pem:
                raise HTTPException(
                    400,
                    "XT expects a plain HMAC secret, not an Ed25519 PEM private key",
                )
            cred.auth_method = "key_secret"
            cred.encrypted_api_key = security.encrypt(body.api_key.strip())
            cred.encrypted_api_secret = security.encrypt(body.api_secret_pem.strip())
            cred.encrypted_token = None

        else:
            raise HTTPException(
                400,
                "auth_method must be 'token', 'key_signature' (nobitex), or 'key_secret' (xt)",
            )

        db.commit()
        return {"saved": True, "auth_method": cred.auth_method, "exchange": ex}
    except security.EncryptionNotConfigured as e:
        raise HTTPException(500, str(e))


@router.delete("")
def delete_credentials(
    exchange: str = Query("nobitex"),
    db: Session = Depends(get_db),
):
    ex = _normalize_exchange(exchange)
    cred = db.query(Credential).filter_by(exchange=ex).first()
    if cred:
        db.delete(cred)
        db.commit()
    return {"deleted": True, "exchange": ex}
