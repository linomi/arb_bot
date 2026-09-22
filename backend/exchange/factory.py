from sqlalchemy.orm import Session
from backend import config_service, security
from backend.models import Credential
from backend.exchange.nobitex import NobitexClient
from backend.exchange.paper import PaperExchangeClient
from backend.bot_state_service import get_or_create_bot_state


def build_market_data_client(db: Session) -> NobitexClient:
    system_cfg = config_service.get_section(db, "system")
    return NobitexClient(base_url=system_cfg.get("base_url", "https://apiv2.nobitex.ir"))


def credentials_configured(db: Session) -> bool:
    cred = db.query(Credential).filter_by(exchange="nobitex").first()
    if cred is None:
        return False
    if cred.auth_method == "token":
        return bool(cred.encrypted_token)
    if cred.auth_method == "key_signature":
        return bool(cred.encrypted_api_key and cred.encrypted_api_secret)
    return bool(cred.encrypted_token)


def build_trading_client(db: Session):
    """
    Paper (default) or live NobitexClient from BotState.trading_mode.
    Live mode currently requires Token auth (Authorization: Token …).
    Key+signature is stored but not yet wired into place_order.
    """
    system_cfg = config_service.get_section(db, "system")
    backbone_cfg = config_service.get_section(db, "backbone")
    md_client = build_market_data_client(db)
    state = get_or_create_bot_state(db)

    if state.trading_mode == "live":
        cred = db.query(Credential).filter_by(exchange="nobitex").first()
        if cred is None:
            raise RuntimeError(
                "Live trading selected but no credentials saved. "
                "Settings → Credentials, or switch back to Paper."
            )
        if cred.auth_method == "key_signature":
            raise RuntimeError(
                "Key+signature auth is saved but not wired for live orders yet. "
                "Use a Nobitex API *token* (auth_method=token) instead."
            )
        if not cred.encrypted_token:
            raise RuntimeError(
                "Live trading selected but no API token is saved. "
                "Add a token in Settings → Credentials, or switch to Paper."
            )
        token = security.decrypt(cred.encrypted_token)
        return NobitexClient(
            base_url=system_cfg.get("base_url", "https://apiv2.nobitex.ir"),
            token=token,
        )

    return PaperExchangeClient(
        market_data_client=md_client,
        fee_rate=backbone_cfg.get("fee_rate", 0.001),
        slippage_rate=backbone_cfg.get("slippage_rate", 0.0005),
    )
