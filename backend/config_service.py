"""
Config is stored in the DB (ConfigBlob, one row per section) so edits made
from the UI take effect immediately, on the next bot cycle, with no
restart. default_config.yaml only seeds the DB on first run.
"""
from pathlib import Path
import yaml
from sqlalchemy.orm import Session
from backend.models import ConfigBlob

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default_config.yaml"
SECTIONS = ("backbone", "init", "system")


def seed_defaults_if_missing(db: Session):
    with open(DEFAULT_CONFIG_PATH) as f:
        defaults = yaml.safe_load(f)
    for section in SECTIONS:
        existing = db.query(ConfigBlob).filter_by(section=section).first()
        if existing is None:
            db.add(ConfigBlob(section=section, data=defaults.get(section, {})))
    db.commit()


def get_section(db: Session, section: str) -> dict:
    row = db.query(ConfigBlob).filter_by(section=section).first()
    if row is None:
        raise ValueError(f"Unknown config section: {section}")
    return dict(row.data)


def get_all(db: Session) -> dict:
    return {s: get_section(db, s) for s in SECTIONS}


def update_section(db: Session, section: str, patch: dict) -> dict:
    row = db.query(ConfigBlob).filter_by(section=section).first()
    if row is None:
        raise ValueError(f"Unknown config section: {section}")
    data = dict(row.data)
    data.update(patch)   # merge -- no hard-coded field whitelist/limits
    row.data = data
    db.commit()
    db.refresh(row)
    return dict(row.data)
