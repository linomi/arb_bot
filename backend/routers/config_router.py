from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import get_db
from backend import config_service
from backend.schemas import ConfigSectionUpdate

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_all_config(db: Session = Depends(get_db)):
    return config_service.get_all(db)


@router.get("/{section}")
def get_section(section: str, db: Session = Depends(get_db)):
    try:
        return config_service.get_section(db, section)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.put("/{section}")
def update_section(section: str, body: ConfigSectionUpdate, db: Session = Depends(get_db)):
    try:
        return config_service.update_section(db, section, body.data)
    except ValueError as e:
        raise HTTPException(404, str(e))
