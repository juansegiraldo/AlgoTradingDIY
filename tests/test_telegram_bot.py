"""Tests for Telegram status formatting."""

from notifications import telegram_bot


def test_format_portfolio_status_separates_initial_and_current_capital(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "get_settings",
        lambda: {
            "initial_capital_gbp": 1000,
            "markets": {"crypto": {"capital_allocation_pct": 50}},
        },
    )
    monkeypatch.setattr(telegram_bot, "get_latest_equity", lambda: {"total_capital": 1058.22})
    monkeypatch.setattr(telegram_bot, "count_open_trades", lambda: 0)
    monkeypatch.setattr(telegram_bot, "get_open_trades", lambda: [])
    monkeypatch.setattr(telegram_bot, "get_total_pnl", lambda: 58.22)
    monkeypatch.setattr(telegram_bot, "get_win_rate", lambda: 100.0)
    monkeypatch.setattr(
        telegram_bot,
        "fetch_quote_balance",
        lambda: {"currency": "GBP", "free": 0, "used": 0, "total": 0},
    )

    text = telegram_bot.format_portfolio_status()

    assert "Capital inicial:</b> GBP 1,000.00" in text
    assert "Capital actual:</b> GBP 1,058.22" in text
    assert "Total: +58.22 GBP (+5.8%)" in text


def test_format_go_live_checklist_highlights_kraken_ready(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "get_settings",
        lambda: {
            "mode": "paper",
            "live_stage": "stage_10",
            "markets": {
                "crypto": {
                    "exchange": "kraken",
                    "pairs": ["BTC/GBP", "ETH/GBP", "SOL/GBP"],
                    "leverage_max": 1,
                    "allow_short": False,
                }
            },
        },
    )
    monkeypatch.setattr(telegram_bot, "get_exchange_name", lambda: "kraken")

    text = telegram_bot.format_go_live_checklist()

    assert "Exchange activo: <b>KRAKEN</b>" in text
    assert "Ejecutar /ready" in text
    assert "No full_auto" in text
    assert "Shorts: NO" in text


def test_format_party_blurb_explains_bot_briefly(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "get_settings",
        lambda: {
            "mode": "paper",
            "markets": {
                "crypto": {
                    "exchange": "kraken",
                    "pairs": ["BTC/GBP", "ETH/GBP", "SOL/GBP"],
                }
            },
        },
    )
    monkeypatch.setattr(telegram_bot, "get_exchange_name", lambda: "kraken")

    text = telegram_bot.format_party_blurb()

    assert "Version fiesta" in text
    assert "GO/SKIP" in text
    assert "sin margen" in text
    assert "PnL neto" in text


def test_format_glossary_defines_scan_terms():
    text = telegram_bot.format_glossary()

    assert "Glosario rapido" in text
    assert "Bullish" in text
    assert "4h" in text
    assert "ATR" in text
    assert "RSI" in text
    assert "EMA" in text
    assert "MACD" in text
    assert "Trigger" in text
    assert "GO/SKIP" in text
    assert "Net" in text


def test_format_scan_report_includes_indicator_details():
    text = telegram_bot.format_scan_report(
        [
            {
                "pair": "BTC/GBP",
                "timeframe": "1h",
                "status": "no_signal",
                "reason": "L1/S0 necesita 3",
                "price": 57000.0,
                "trend": "mixed",
                "volatility": {"atr_pct": 0.42},
                "rsi": {"triggered": True, "signal": "long", "value": 51.2},
                "ema": {"triggered": False, "signal": None},
                "macd": {"triggered": False, "signal": None},
                "volume": {"triggered": True, "signal": "confirm", "value": {"ratio": 1.8}},
            }
        ],
        [],
    )

    assert "SCAN MANUAL" in text
    assert "Alertas GO/SKIP nuevas: <b>0</b>" in text
    assert "BTC/GBP 1h" in text
    assert "ATR 0.42%" in text
    assert "RSI L" in text
    assert "Vol ok (1.80x)" in text
    assert "L1/S0 necesita 3" in text


def test_format_open_positions_uses_active_exchange_when_empty(monkeypatch):
    monkeypatch.setattr(telegram_bot, "get_open_trades", lambda: [])
    monkeypatch.setattr(telegram_bot, "get_exchange_name", lambda: "kraken")

    text = telegram_bot.format_open_positions()

    assert "Sin posiciones abiertas en KRAKEN" in text
