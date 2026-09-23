"""
Config is stored in the DB (ConfigBlob, one row per section).
default_config.yaml only seeds the DB on first run.
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


def _validate_backbone(data: dict):
    z_entry = float(data.get("z_entry", 2))
    z_close = float(data.get("z_close", 0.5))
    z_stop = float(data.get("z_stop_loss", 3.5))
    if z_close < 0 or z_entry < 0 or z_stop < 0:
        raise ValueError("z thresholds must be >= 0")
    if not (z_stop > z_entry > z_close):
        raise ValueError(
            f"require z_stop_loss ({z_stop}) > z_entry ({z_entry}) > z_close ({z_close})"
        )
    fee = float(data.get("fee_rate", 0))
    slip = float(data.get("slippage_rate", 0))
    if fee < 0 or slip < 0:
        raise ValueError("fee_rate and slippage_rate must be >= 0")
    tn = float(data.get("trade_notional", 1))
    if tn <= 0:
        raise ValueError("trade_notional must be > 0")
    ws = int(data.get("window_size", 100))
    if ws < 30:
        raise ValueError("window_size must be >= 30")
    st = int(data.get("sampling_time", 60))
    if st < 1:
        raise ValueError("sampling_time must be >= 1")


def _validate_init(data: dict):
    mn = int(data.get("min_group_size", 2))
    mx = int(data.get("max_group_size", 5))
    if mn < 2:
        raise ValueError("min_group_size must be >= 2")
    if mx < mn:
        raise ValueError("max_group_size must be >= min_group_size")
    keep = int(data.get("keep_top_n", 10))
    if keep < 1:
        raise ValueError("keep_top_n must be >= 1")


def update_section(db: Session, section: str, patch: dict) -> dict:
    row = db.query(ConfigBlob).filter_by(section=section).first()
    if row is None:
        raise ValueError(f"Unknown config section: {section}")
    data = dict(row.data)
    data.update(patch)
    if section == "backbone":
        _validate_backbone(data)
    elif section == "init":
        _validate_init(data)
    row.data = data
    db.commit()
    db.refresh(row)
    return dict(row.data)
