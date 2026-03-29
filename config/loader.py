"""Configuration loader - reads YAML files and provides access to settings.

Secrets priority:
  1. Environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
     BINANCE_API_KEY, BINANCE_API_SECRET)
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
    return _load_yaml("settings.yaml")


def _load_secrets_with_env_override() -> dict:
    """Load secrets.yaml then override with env vars if present."""
    try:
        secrets = _load_yaml("secrets.yaml")
    except FileNotFoundError:
        secrets = {}

    # Ensure nested dicts exist
    secrets.setdefault("telegram", {})
    secrets.setdefault("binance_testnet", {})

    # Environment variable overrides
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        secrets["telegram"]["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        secrets["telegram"]["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("BINANCE_API_KEY"):
        secrets["binance_testnet"]["api_key"] = os.environ["BINANCE_API_KEY"]
    if os.environ.get("BINANCE_API_SECRET"):
        secrets["binance_testnet"]["api_secret"] = os.environ["BINANCE_API_SECRET"]

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
