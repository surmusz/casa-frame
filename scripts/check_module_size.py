#!/usr/bin/env python3
"""CI 门禁：casa/**/*.py 单文件行数上限。"""
from __future__ import annotations

import sys
from pathlib import Path

MAX_LINES = 600
SKIP: set[str] = set()


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "casa"
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in SKIP:
            continue
        count = len(path.read_text(encoding="utf-8").splitlines())
        if count > MAX_LINES:
            failures.append(f"{path.relative_to(root.parent)}: {count} lines (max {MAX_LINES})")
    if failures:
        print("Module size check FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"Module size check OK (max {MAX_LINES} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
