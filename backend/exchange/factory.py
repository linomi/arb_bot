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

    Live supports:
      - token: Authorization: Token …
      - key_signature: Nobitex-Key + Ed25519 signature headers
    """
    system_cfg = config_service.get_section(db, "system")
    backbone_cfg = config_service.get_section(db, "backbone")
    md_client = build_market_data_client(db)
    state = get_or_create_bot_state(db)
    base = system_cfg.get("base_url", "https://apiv2.nobitex.ir")

    if state.trading_mode == "live":
        cred = db.query(Credential).filter_by(exchange="nobitex").first()
        if cred is None:
            raise RuntimeError(
                "Live trading selected but no credentials saved. "
                "Settings → Credentials, or switch back to Paper."
            )

        if cred.auth_method == "key_signature":
            if not cred.encrypted_api_key or not cred.encrypted_api_secret:
                raise RuntimeError(
                    "API Key auth selected but key/secret missing. "
                    "Re-save both apiKey and secretKey under Settings."
                )
            api_key = security.decrypt(cred.encrypted_api_key)
            private_key = security.decrypt(cred.encrypted_api_secret)
            return NobitexClient(
                base_url=base,
                api_key=api_key,
                private_key=private_key,
            )

        # default / token
        if not cred.encrypted_token:
            raise RuntimeError(
                "Live trading selected but no API token is saved. "
                "Add a token OR use Auth Method = API Key + Secret with both fields."
            )
        token = security.decrypt(cred.encrypted_token)
        return NobitexClient(base_url=base, token=token)

    return PaperExchangeClient(
        market_data_client=md_client,
        fee_rate=backbone_cfg.get("fee_rate", 0.001),
        slippage_rate=backbone_cfg.get("slippage_rate", 0.0005),
    )
