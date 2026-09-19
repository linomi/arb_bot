from sqlalchemy.orm import Session
from backend import config_service, security
from backend.models import Credential
from backend.exchange.nobitex import NobitexClient
from backend.exchange.paper import PaperExchangeClient
from backend.bot_state_service import get_or_create_bot_state


def build_market_data_client(db: Session) -> NobitexClient:
    system_cfg = config_service.get_section(db, "system")
    return NobitexClient(base_url=system_cfg.get("base_url", "https://apiv2.nobitex.ir"))


def build_trading_client(db: Session):
    """
    Returns either a PaperExchangeClient (default, always safe) or a live
    NobitexClient with credentials attached, based on BotState.trading_mode
    (the single source of truth for paper/live -- set via POST /api/bot/mode).
    """
    system_cfg = config_service.get_section(db, "system")
    backbone_cfg = config_service.get_section(db, "backbone")
    md_client = build_market_data_client(db)
    state = get_or_create_bot_state(db)

    if state.trading_mode == "live":
        cred = db.query(Credential).filter_by(exchange="nobitex").first()
        if cred is None or not cred.encrypted_token:
            raise RuntimeError(
                "Live trading is selected but no API token is saved. "
                "Add one in Settings -> Credentials, or switch back to Paper mode."
            )
        token = security.decrypt(cred.encrypted_token)
        live_client = NobitexClient(base_url=system_cfg.get("base_url"), token=token)
        return live_client

    return PaperExchangeClient(
        market_data_client=md_client,
        fee_rate=backbone_cfg.get("fee_rate", 0.001),
        slippage_rate=backbone_cfg.get("slippage_rate", 0.0005),
    )
