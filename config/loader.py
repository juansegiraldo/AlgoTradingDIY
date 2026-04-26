"""Configuration loader - reads YAML files and provides access to settings.

Secrets priority:
  1. Environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
     BINANCE_API_KEY, BINANCE_API_SECRET, KRAKEN_API_KEY, KRAKEN_API_SECRET)
  2. config/secrets.yaml (fallback for local dev)
"""

import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"


def _load_yaml(filename: str) -> dict:
    filepath = CONFIG_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(f"Config file not found: {filepath}")
    with open(filepath, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict:
    settings = _load_yaml("settings.yaml")
    settings.setdefault("live_stage", "stage_10")
    deployment = settings.setdefault("live_deployment", {})
    deployment.setdefault("require_manual_confirmation", True)
    deployment.setdefault("auto_pause_on_live_failures", True)
    deployment.setdefault("morning_balance_source", "kraken")
    deployment.setdefault("readiness_check_timeout_seconds", 15)
    profiles = deployment.setdefault("stage_profiles", {})
    profiles.setdefault(
        "stage_10",
        {
            "label": "10 GBP",
            "max_operable_capital_gbp": 10,
            "risk_per_trade_pct": 5.0,
            "max_simultaneous_positions": 1,
            "leverage_max": 1,
            "allow_partial_tp": False,
            "allowed_order_types": ["market"],
        },
    )
    profiles.setdefault(
        "stage_100",
        {
            "label": "100 GBP",
            "max_operable_capital_gbp": 100,
            "risk_per_trade_pct": 0.5,
            "max_simultaneous_positions": 2,
            "leverage_max": 1,
            "allow_partial_tp": True,
            "allowed_order_types": ["market"],
        },
    )
    profiles.setdefault(
        "stage_1000",
        {
            "label": "1000 GBP",
            "max_operable_capital_gbp": 1000,
            "risk_per_trade_pct": 1.0,
            "max_simultaneous_positions": 3,
            "leverage_max": 1,
            "allow_partial_tp": True,
            "allowed_order_types": ["market"],
        },
    )
    return settings


def _load_secrets_with_env_override() -> dict:
    """Load secrets.yaml then override with env vars if present."""
    try:
        secrets = _load_yaml("secrets.yaml")
    except FileNotFoundError:
        secrets = {}

    # Ensure nested dicts exist
    secrets.setdefault("telegram", {})
    secrets.setdefault("binance", {})
    secrets.setdefault("binance_testnet", {})
    secrets.setdefault("kraken", {})

    # Environment variable overrides
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        secrets["telegram"]["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        secrets["telegram"]["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("BINANCE_API_KEY"):
        secrets["binance"]["api_key"] = os.environ["BINANCE_API_KEY"]
        secrets["binance_testnet"]["api_key"] = os.environ["BINANCE_API_KEY"]
    if os.environ.get("BINANCE_API_SECRET"):
        secrets["binance"]["api_secret"] = os.environ["BINANCE_API_SECRET"]
        secrets["binance_testnet"]["api_secret"] = os.environ["BINANCE_API_SECRET"]
    if os.environ.get("KRAKEN_API_KEY"):
        secrets["kraken"]["api_key"] = os.environ["KRAKEN_API_KEY"]
    if os.environ.get("KRAKEN_API_SECRET"):
        secrets["kraken"]["api_secret"] = os.environ["KRAKEN_API_SECRET"]

    return secrets


def load_secrets() -> dict:
    return _load_secrets_with_env_override()


def load_risk_policies() -> dict:
    return _load_yaml("risk_policies.yaml")


# Singletons loaded on first import
_settings = None
_secrets = None
_risk_policies = None


def get_settings() -> dict:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def get_secrets() -> dict:
    global _secrets
    if _secrets is None:
        _secrets = load_secrets()
    return _secrets


def get_risk_policies() -> dict:
    global _risk_policies
    if _risk_policies is None:
        _risk_policies = load_risk_policies()
    return _risk_policies


def get_gbp_usd_rate() -> float:
    """
    Return the GBP/USD conversion rate used for non-GBP quote currencies.

    Priority:
      1. GBP_USD_RATE environment variable
      2. settings.yaml -> fx.gbp_usd_rate

    The rate is intentionally not hardcoded in code because it changes over time.
    """
    raw = os.environ.get("GBP_USD_RATE")
    if raw is None:
        raw = get_settings().get("fx", {}).get("gbp_usd_rate")

    if raw in (None, ""):
        raise ValueError("GBP/USD rate not configured. Set GBP_USD_RATE or fx.gbp_usd_rate")

    try:
        rate = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid GBP/USD rate: {raw}") from exc

    if rate <= 0:
        raise ValueError(f"Invalid GBP/USD rate: {rate}")
    return rate


def get_live_stage() -> str:
    return get_settings().get("live_stage", "stage_10")


def get_live_stage_profile() -> dict:
    settings = get_settings()
    deployment = settings.get("live_deployment", {})
    profiles = deployment.get("stage_profiles", {})
    stage = settings.get("live_stage", "stage_10")
    return profiles.get(stage, profiles.get("stage_10", {}))


def get_dynamic_limits(capital_gbp: float) -> dict:
    """
    Retorna límites de posición basados en el capital actual.

    Solo aplica en live mode. En paper mode los callers deben usar valores estáticos.

    Returns:
        {
            "max_simultaneous_positions": int,
            "max_position_size_pct": float,   # % de la asignación del mercado
            "tier_label": str,
            "capital_gbp": float,
        }
    """
    settings = get_settings()
    tiers = settings.get("live_deployment", {}).get("capital_scaling_tiers", [])

    if not tiers:
        profile = get_live_stage_profile()
        pm = settings.get("position_management", {})
        return {
            "max_simultaneous_positions": int(profile.get("max_simultaneous_positions", 1)),
            "max_position_size_pct": float(pm.get("max_position_size_pct", 20)),
            "tier_label": profile.get("label", "Unknown"),
            "capital_gbp": capital_gbp,
        }

    matched = tiers[0]
    for tier in tiers:
        if capital_gbp >= float(tier.get("min_capital_gbp", 0)):
            matched = tier

    return {
        "max_simultaneous_positions": int(matched.get("max_simultaneous_positions", 1)),
        "max_position_size_pct": float(matched.get("max_position_size_pct", 20)),
        "tier_label": str(matched.get("label", "Unknown")),
        "capital_gbp": capital_gbp,
    }
