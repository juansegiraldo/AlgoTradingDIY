"""Configuration loader - reads YAML files and provides access to settings."""

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


def load_secrets() -> dict:
    return _load_yaml("secrets.yaml")


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
