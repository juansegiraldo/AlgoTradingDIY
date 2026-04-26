"""Run pytest with stdlib trace-based coverage enforcement."""

from __future__ import annotations

import os
import sys
import trace
from pathlib import Path

os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_FILES = [
    PROJECT_ROOT / "risk" / "position_sizer.py",
    PROJECT_ROOT / "risk" / "risk_manager.py",
    PROJECT_ROOT / "signals" / "signal_generator.py",
]
COVERAGE_WINDOWS = {
    "risk/position_sizer.py": [(44, 170)],
    "risk/risk_manager.py": [(54, 189), (192, 240)],
    "signals/signal_generator.py": [(24, 195)],
}
COVERAGE_THRESHOLD = 0.80


def _count_executable_lines(path: Path) -> set[int]:
    executable: set[int] = set()
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    windows = COVERAGE_WINDOWS.get(rel)
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if windows and not any(start <= idx <= end for start, end in windows):
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        executable.add(idx)
    return executable


def _gather_counts(results: trace.CoverageResults, path: Path) -> set[int]:
    executed: set[int] = set()
    for (stored_path, lineno), hit in results.counts.items():
        if hit <= 0:
            continue
        try:
            if Path(stored_path).resolve() == path:
                executed.add(lineno)
        except (TypeError, OSError):
            continue
    return executed


def main() -> int:
    tracer = trace.Trace(count=True, trace=False, ignoredirs=[sys.prefix, str(PROJECT_ROOT / ".venv")])
    exit_code = tracer.runfunc(pytest.main, [])
    results = tracer.results()

    coverage_failures: list[str] = []
    for file_path in TARGET_FILES:
        executed = _gather_counts(results, file_path.resolve())
        executable = _count_executable_lines(file_path)
        if not executable:
            continue
        ratio = len(executed & executable) / len(executable)
        percent = ratio * 100
        print(f"{file_path.relative_to(PROJECT_ROOT)} coverage: {percent:.1f}% ({len(executed & executable)}/{len(executable)} lines)")
        if ratio < COVERAGE_THRESHOLD:
            coverage_failures.append(f"{file_path.name}: {percent:.1f}% < {COVERAGE_THRESHOLD * 100:.0f}%")

    if exit_code != 0:
        return exit_code
    if coverage_failures:
        print("Coverage check failed:", "; ".join(coverage_failures))
        return 1
    print("Coverage threshold satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
