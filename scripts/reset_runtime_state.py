"""Reset local runtime trading state for a clean Kraken paper/live start.

This script clears the local SQLite runtime DB state only. It does not touch
config/secrets.yaml and it does not send any order to Kraken.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.loader import get_settings
from data.database import reset_runtime_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset local trading runtime state")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required. Confirms deletion of local trades/snapshots/logs/flags.",
    )
    args = parser.parse_args()

    if not args.confirm:
        print("Refusing to reset without --confirm.")
        print("Run: python scripts/reset_runtime_state.py --confirm")
        return 2

    settings = get_settings()
    mode = str(settings.get("mode", "paper"))
    crypto_cfg = settings.get("markets", {}).get("crypto", {})
    exchange = str(crypto_cfg.get("exchange", "kraken")).lower()
    allocation_pct = float(crypto_cfg.get("capital_allocation_pct", 100) or 100)
    initial_capital = float(settings.get("initial_capital_gbp", 1000) or 1000)
    crypto_capital = initial_capital * (allocation_pct / 100.0)

    summary = reset_runtime_state(
        total_capital=initial_capital,
        mode=mode,
        source=f"{exchange}_reset",
        crypto_capital=crypto_capital,
        free_balance_gbp=initial_capital if mode == "paper" else 0.0,
        margin_used_gbp=0.0,
        note=(
            f"Runtime state reset for {exchange.upper()} startup. "
            "Local DB cleared; exchange account and secrets untouched."
        ),
    )

    print("Runtime state reset complete.")
    print(f"Exchange: {exchange}")
    print(f"Mode: {summary['mode']}")
    print(f"Total capital snapshot: GBP {summary['total_capital']:.2f}")
    print(f"Free balance snapshot: GBP {summary['free_balance_gbp']:.2f}")
    print("Rows before reset:")
    for table, count in summary["before"].items():
        print(f"  {table}: {count}")
    print("Rows after reset:")
    for table, count in summary["after"].items():
        print(f"  {table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
