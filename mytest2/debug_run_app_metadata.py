#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debug_run_app_metadata.py

What this script does:
- Read one app's metadata row from a CSV file (matched by `appId` column).
- Call GPTClient.analyze_app_metadata(...) to generate a compact app summary.
- Save the structured result to local JSON for inspection.

Inputs:
- --csv-path:
  Path to metadata CSV. Example: F:\\workplace\\framework\\APP_csv\\xxx.csv
- --app-id:
  Package id to match, using CSV column `appId`.
- --api-key (optional):
  OpenAI key. If omitted, fallback to OPENAI_API_KEY from environment/.env.
- --model / --temperature / --timeout-s:
  LLM call settings.
- --out (optional):
  Output JSON path. Default:
  mytest2/metadata_debug/<timestamp>_<appId>.json

Output:
- A JSON file containing:
  - app_id
  - app_intro
  - focus_hints
  - questionnaire_type
  - notes
  - _debug metadata (csv path, matched column list)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from gpt_cls import GPTClient  # noqa: E402
from utils import (  # noqa: E402
    clear_time_consumed,
    clear_token_record,
    get_time_consumed,
    get_token_consumed,
)


# Default debug inputs (can still be overridden by CLI args).
DEFAULT_CSV_PATH = str(PROJECT_ROOT / "APP_csv" / "commonsense-256app.csv")
DEFAULT_APP_ID = "com.beenverified.android"

# Only pass selected metadata fields to LLM, not the whole CSV row.
SELECTED_METADATA_FIELDS = [
    "description",
    "descriptionHTML",
    "summary",
    "contentRating",
    "contentRatingDescription",
    "offersIAP",
    "inAppProductPrice",
    "genre",
    "genreId",
    "categories",
]


def _read_csv_rows(csv_path: Path) -> List[Dict[str, Any]]:
    """
    Input:
    - csv_path: CSV file path.
    Output:
    - List of dict rows.
    """
    encodings = ("utf-8-sig", "utf-8", "gb18030")
    last_err: Exception | None = None
    for enc in encodings:
        try:
            with csv_path.open("r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                return [dict(row) for row in reader]
        except Exception as e:  # pragma: no cover - fallback path
            last_err = e
    raise RuntimeError(f"Failed to read CSV: {csv_path} ({last_err})")


def _find_row_by_app_id(rows: List[Dict[str, Any]], app_id: str) -> Dict[str, Any]:
    """
    Input:
    - rows: CSV rows.
    - app_id: package id to find.
    Output:
    - First matched row whose `appId` equals app_id.
    """
    target = (app_id or "").strip()
    for row in rows:
        if str(row.get("appId", "")).strip() == target:
            return row
    raise KeyError(f"appId not found in CSV: {app_id}")


def _pick_selected_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Input:
    - row: raw CSV row dict.
    Output:
    - Dict with only selected metadata fields used by analyze_app_metadata.
    """
    out: Dict[str, Any] = {}
    for key in SELECTED_METADATA_FIELDS:
        out[key] = row.get(key, "")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Debug app metadata summarization via GPT.")
    parser.add_argument("--csv-path", default=DEFAULT_CSV_PATH, help="Path to app metadata CSV")
    parser.add_argument("--app-id", default=DEFAULT_APP_ID, help="Package id (match CSV column appId)")
    parser.add_argument("--api-key", default=None, help="OpenAI API key; fallback to OPENAI_API_KEY in .env")
    parser.add_argument("--model", default="gpt-4o", help="Model name")
    parser.add_argument("--temperature", type=float, default=0.1, help="Sampling temperature")
    parser.add_argument("--timeout-s", type=int, default=90, help="LLM request timeout seconds")
    parser.add_argument("--out", default="", help="Output JSON path")
    args = parser.parse_args()

    csv_path = Path(args.csv_path).resolve()
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    api_key = args.api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Set it in .env or pass --api-key.")

    rows = _read_csv_rows(csv_path)
    row = _find_row_by_app_id(rows, args.app_id)
    selected_metadata = _pick_selected_metadata(row)

    gpt = GPTClient(
        api_key=api_key,
        model=str(args.model),
        temperature=float(args.temperature),
        timeout_s=int(args.timeout_s),
    )
    clear_time_consumed()
    clear_token_record()
    result = gpt.analyze_app_metadata(app_id=args.app_id, app_metadata=selected_metadata)
    time_list = get_time_consumed("GPTClient.analyze_app_metadata")
    token_list = get_token_consumed("analyze_app_metadata")
    elapsed_s = float(time_list[-1]) if time_list else None
    prompt_tokens = int(token_list[-1][0]) if token_list else None
    completion_tokens = int(token_list[-1][1]) if token_list else None
    total_tokens = (prompt_tokens + completion_tokens) if (prompt_tokens is not None and completion_tokens is not None) else None

    out_path = Path(args.out).resolve() if str(args.out).strip() else (
        PROJECT_ROOT
        / "mytest2"
        / "metadata_debug"
        / f"{int(time.time() * 1000)}_{args.app_id.replace('.', '_')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.model_dump(mode="json")
    payload["_debug"] = {
        "csv_path": str(csv_path),
        "matched_app_id": args.app_id,
        "csv_columns": sorted(list(row.keys())),
        "selected_metadata_fields": SELECTED_METADATA_FIELDS,
        "elapsed_seconds": elapsed_s,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] app_id={args.app_id}")
    print(f"[OK] output={out_path}")
    print(f"[OK] app_intro={payload.get('app_intro', '')}")
    print(f"[OK] questionnaire_type={payload.get('questionnaire_type', '')}")
    print(f"[METRIC] elapsed_seconds={elapsed_s}")
    print(f"[METRIC] prompt_tokens={prompt_tokens} completion_tokens={completion_tokens} total_tokens={total_tokens}")


if __name__ == "__main__":
    main()
