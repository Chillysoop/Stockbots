"""Which broker each bot trades on.

Paper (the default): every bot trades on its own Alpaca paper account, the one
pasted on the Setup page. Optional: PAPER_BROKER_<BOT>=tradier runs it on Tradier's
sandbox instead (TRADIER_SANDBOX_TOKEN_<BOT>), to test that broker with fake money.

Live (MODE=live and LIVE_ACK set): each bot uses LIVE_BROKER_<BOT>:
  alpaca  -> ALPACA_LIVE_KEY_<BOT>, ALPACA_LIVE_SECRET_<BOT>
  tradier -> TRADIER_TOKEN_<BOT> (and optionally TRADIER_ACCOUNT_<BOT>)
  public  -> PUBLIC_SECRET_<BOT> (and optionally PUBLIC_ACCOUNT_<BOT>)
  off     -> the bot doesn't run

Market data always comes from the bot's Alpaca paper keys, so all bots see the
same prices whichever broker holds their money. Live settings are server
variables on purpose: there is no button that turns on real money.
"""
from __future__ import annotations

from dataclasses import dataclass

from .alpaca import AlpacaClient
from .config import Config, env
from .publicapi import PublicClient
from .tradier import TradierClient

BROKERS = ("alpaca", "tradier", "public")


@dataclass
class BrokerChoice:
    client: object | None
    broker: str  # alpaca | tradier | public | off
    label: str  # shown on the dashboard
    problem: str | None = None  # why the bot can't run, if it can't


def _var(name: str, bot_id: str) -> str | None:
    return env(f"{name}_{bot_id.upper()}")


def choose(bot_id: str, cfg: Config, paper_keys: dict, enabled: bool = True) -> BrokerChoice:
    key, secret = paper_keys.get("alpaca_key"), paper_keys.get("alpaca_secret")
    if not enabled:
        return BrokerChoice(None, "off", "Off")
    data = AlpacaClient(key, secret, live=False) if key and secret else None

    if not cfg.live:
        broker = (_var("PAPER_BROKER", bot_id) or "alpaca").lower()
        if broker == "tradier":
            token = _var("TRADIER_SANDBOX_TOKEN", bot_id)
            if not token or not data:
                return BrokerChoice(None, "tradier", "Tradier sandbox",
                                    "needs TRADIER_SANDBOX_TOKEN and its Alpaca paper keys for market data")
            return BrokerChoice(
                TradierClient(token, data, account_id=_var("TRADIER_SANDBOX_ACCOUNT", bot_id), live=False,
                              state_dir=cfg.data_dir),
                "tradier", "Tradier sandbox")
        if not data:
            return BrokerChoice(None, "alpaca", "Alpaca paper", "no Alpaca paper keys")
        return BrokerChoice(AlpacaClient(key, secret, live=False), "alpaca", "Alpaca paper")

    broker = (_var("LIVE_BROKER", bot_id) or "alpaca").lower()
    if broker == "off":
        return BrokerChoice(None, "off", "Off (live)")
    if broker not in BROKERS:
        return BrokerChoice(None, broker, broker, f"LIVE_BROKER must be one of {', '.join(BROKERS)} or off")
    if broker == "alpaca":
        lk = _var("ALPACA_LIVE_KEY", bot_id) or _var("ALPACA_KEY", bot_id)
        ls = _var("ALPACA_LIVE_SECRET", bot_id) or _var("ALPACA_SECRET", bot_id)
        if not lk or not ls:
            return BrokerChoice(None, "alpaca", "Alpaca live", "needs ALPACA_LIVE_KEY and ALPACA_LIVE_SECRET")
        return BrokerChoice(AlpacaClient(lk, ls, live=True), "alpaca", "Alpaca live")
    if not data:
        return BrokerChoice(None, broker, broker.title() + " live",
                            "needs this bot's Alpaca paper keys (Setup) for market data")
    if broker == "tradier":
        token = _var("TRADIER_TOKEN", bot_id)
        if not token:
            return BrokerChoice(None, "tradier", "Tradier live", "needs TRADIER_TOKEN")
        return BrokerChoice(
            TradierClient(token, data, account_id=_var("TRADIER_ACCOUNT", bot_id), live=True, state_dir=cfg.data_dir),
            "tradier", "Tradier live")
    secret = _var("PUBLIC_SECRET", bot_id)
    if not secret:
        return BrokerChoice(None, "public", "Public live", "needs PUBLIC_SECRET")
    return BrokerChoice(
        PublicClient(secret, data, account_id=_var("PUBLIC_ACCOUNT", bot_id), state_dir=cfg.data_dir),
        "public", "Public live")
