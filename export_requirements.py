from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def export_requirements(out_path: Path) -> int:
    cmd = [sys.executable, "-m", "pip", "freeze"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.stderr.write(result.stderr or "pip freeze failed\n")
        return result.returncode

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.stdout, encoding="utf-8")
    print(f"requirements exported to: {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Export current environment packages to a requirements file.")
    parser.add_argument("--out", default="requirements-current.txt", help="Output requirements file path")
    args = parser.parse_args()
    return export_requirements(Path(args.out).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
