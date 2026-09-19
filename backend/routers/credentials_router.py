from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models import Credential
from backend.schemas import CredentialIn
from backend import security

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


@router.get("/status")
def credential_status(db: Session = Depends(get_db)):
    cred = db.query(Credential).filter_by(exchange="nobitex").first()
    if not cred:
        return {"configured": False, "auth_method": None, "exchange": "nobitex"}
    configured = bool(cred.encrypted_token or (cred.encrypted_api_key and cred.encrypted_api_secret))
    return {"configured": configured, "auth_method": cred.auth_method, "exchange": "nobitex"}


@router.post("")
def save_credentials(body: CredentialIn, db: Session = Depends(get_db)):
    try:
        cred = db.query(Credential).filter_by(exchange="nobitex").first()
        if cred is None:
            cred = Credential(exchange="nobitex")
            db.add(cred)

        cred.auth_method = body.auth_method
        if body.auth_method == "token":
            if not body.token:
                raise HTTPException(400, "token is required for auth_method=token")
            cred.encrypted_token = security.encrypt(body.token)
            cred.encrypted_api_key = None
            cred.encrypted_api_secret = None
        elif body.auth_method == "key_signature":
            if not body.api_key or not body.api_secret_pem:
                raise HTTPException(400, "api_key and api_secret_pem are required for auth_method=key_signature")
            cred.encrypted_api_key = security.encrypt(body.api_key)
            cred.encrypted_api_secret = security.encrypt(body.api_secret_pem)
            cred.encrypted_token = None
        else:
            raise HTTPException(400, "auth_method must be 'token' or 'key_signature'")

        db.commit()
        return {"saved": True, "auth_method": cred.auth_method}
    except security.EncryptionNotConfigured as e:
        raise HTTPException(500, str(e))


@router.delete("")
def delete_credentials(db: Session = Depends(get_db)):
    cred = db.query(Credential).filter_by(exchange="nobitex").first()
    if cred:
        db.delete(cred)
        db.commit()
    return {"deleted": True}
