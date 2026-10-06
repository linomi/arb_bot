"""
Config is stored in the DB (ConfigBlob, one row per section).
default_config.yaml only seeds the DB on first run; missing keys in an
existing section are filled additively from defaults (never overwrite user values).
"""
from pathlib import Path
import yaml
from sqlalchemy.orm import Session
from backend.models import ConfigBlob

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default_config.yaml"
SECTIONS = ("backbone", "init", "system")


def _load_yaml_defaults() -> dict:
    with open(DEFAULT_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def seed_defaults_if_missing(db: Session):
    defaults = _load_yaml_defaults()
    for section in SECTIONS:
        existing = db.query(ConfigBlob).filter_by(section=section).first()
        section_defaults = dict(defaults.get(section, {}) or {})
        if existing is None:
            db.add(ConfigBlob(section=section, data=section_defaults))
        else:
            # Additive merge: fill keys present in yaml but missing in DB
            data = dict(existing.data or {})
            changed = False
            for k, v in section_defaults.items():
                if k not in data:
                    data[k] = v
                    changed = True
            if changed:
                existing.data = data
    db.commit()


def get_section(db: Session, section: str) -> dict:
    row = db.query(ConfigBlob).filter_by(section=section).first()
    if row is None:
        raise ValueError(f"Unknown config section: {section}")
    data = dict(row.data or {})
    # Fill missing keys from defaults at read time (no write unless seed was run)
    defaults = _load_yaml_defaults().get(section, {}) or {}
    for k, v in defaults.items():
        if k not in data:
            data[k] = v
    return data


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
    tpr = float(data.get("target_profit_rate", 0.0))
    if tpr < 0 or tpr >= 1:
        raise ValueError("target_profit_rate must be in [0, 1)")
    sm = str(data.get("stationarity_method", "engle_granger"))
    if sm not in ("engle_granger", "adf_kpss"):
        raise ValueError("stationarity_method must be 'engle_granger' or 'adf_kpss'")
    for k in ("entry_retry_cooldown_sec", "max_holding_hours", "fit_log_interval_sec"):
        if float(data.get(k, 0) or 0) < 0:
            raise ValueError(f"{k} must be >= 0")
    mes = data.get("max_entry_scale", 3.0)
    if mes is not None and mes != "":
        mes_f = float(mes)
        # 0 or None disables the guard; otherwise require >= 1.
        if mes_f != 0 and mes_f < 1:
            raise ValueError("max_entry_scale must be >= 1 (or 0/None to disable)")


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
