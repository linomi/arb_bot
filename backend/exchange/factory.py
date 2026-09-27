from sqlalchemy.orm import Session
from backend import config_service, security
from backend.models import Credential
from backend.exchange.nobitex import NobitexClient
from backend.exchange.paper import PaperExchangeClient
from backend.bot_state_service import get_or_create_bot_state

SUPPORTED_EXCHANGES = ("nobitex", "xt")


def _active_exchange(db: Session) -> str:
    """Single active exchange is selected via BotState.exchange (default nobitex)."""
    state = get_or_create_bot_state(db)
    ex = (getattr(state, "exchange", None) or "nobitex").strip().lower()
    if ex not in SUPPORTED_EXCHANGES:
        return "nobitex"
    return ex


def build_market_data_client(db: Session):
    """
    Public market-data client for the active exchange.
    Paper mode wraps this client; live trading uses the same source for OHLC.
    """
    exchange = _active_exchange(db)
    system_cfg = config_service.get_section(db, "system")

    if exchange == "xt":
        from backend.exchange.xt import XTClient
        return XTClient(api_key=None, api_secret=None)  # public endpoints only

    return NobitexClient(base_url=system_cfg.get("base_url", "https://apiv2.nobitex.ir"))


def credentials_configured(db: Session, exchange: str | None = None) -> bool:
    ex = (exchange or _active_exchange(db)).strip().lower()
    cred = db.query(Credential).filter_by(exchange=ex).first()
    if cred is None:
        return False
    if cred.auth_method == "token":
        return bool(cred.encrypted_token)
    if cred.auth_method in ("key_signature", "key_secret"):
        return bool(cred.encrypted_api_key and cred.encrypted_api_secret)
    return bool(cred.encrypted_token)


def build_trading_client(db: Session):
    """
    Paper (default) or live client from BotState.trading_mode + BotState.exchange.

    Live Nobitex supports token / key_signature.
    Live XT supports key_secret (HMAC apiKey + secret).
    """
    system_cfg = config_service.get_section(db, "system")
    backbone_cfg = config_service.get_section(db, "backbone")
    state = get_or_create_bot_state(db)
    exchange = _active_exchange(db)
    md_client = build_market_data_client(db)

    if state.trading_mode == "live":
        cred = db.query(Credential).filter_by(exchange=exchange).first()
        if cred is None:
            raise RuntimeError(
                f"Live trading selected for {exchange} but no credentials saved. "
                "Settings → Credentials, or switch back to Paper."
            )

        if exchange == "xt":
            from backend.exchange.xt import XTClient
            if cred.auth_method != "key_secret" or not (
                cred.encrypted_api_key and cred.encrypted_api_secret
            ):
                raise RuntimeError(
                    "XT live trading requires auth_method=key_secret with api_key + secret."
                )
            api_key = security.decrypt(cred.encrypted_api_key)
            api_secret = security.decrypt(cred.encrypted_api_secret)
            return XTClient(
                api_key=api_key,
                api_secret=api_secret,
                leverage=int(backbone_cfg.get("xt_leverage", 2)),
                margin_mode=str(backbone_cfg.get("xt_margin_mode", "isolated")),
            )

        # nobitex
        base = system_cfg.get("base_url", "https://apiv2.nobitex.ir")
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
