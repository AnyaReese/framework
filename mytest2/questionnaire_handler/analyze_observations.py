#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Purpose:
Analyze questionnaire observation files from one trace run and summarize:
1) UI-level: each UI hit which blocks, and which block questions were effectively triggered.
2) APP-level: union summary across all UIs.

Inputs:
- --trace-run-dir:
  Path to one trace run directory, e.g.:
  traces/20260422_140852_com.marktka.calculatorYou
- --output (optional):
  Output JSON path. Default:
  <trace-run-dir>/observations_analysis.json
- --inactive-values (optional):
  Comma-separated values treated as "not triggered", default includes:
  no, none, never, unknown, not_shown, ...

How it works:
1) Locate observation root:
   - prefer <run>/observations
   - fallback <run>/questionnaire2_observations
2) Read:
   - router/*.json
   - blocks_fill/*.json
3) Build UI records:
   - blocks_fill files are primary records (they already contain matched_block_ids).
   - router-only records are added when a state_sig has no fill record.
4) Effective trigger rule (for each proposed update):
   - new_answer is non-empty
   - and not a placeholder/inactive value
5) Write one JSON report with "app_level" and "ui_level".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


DEFAULT_INACTIVE_VALUES = {
    "no",
    "none",
    "never",
    "unknown",
    "uncertain",
    "not_shown",
    "not shown",
    "n/a",
    "na",
    "false",
    "0",
    "",
}


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """
    Input:
    - path: JSON file path.
    Output:
    - Parsed dict, or None when parse fails.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalize_str(x: Any) -> str:
    """
    Input:
    - Any value.
    Output:
    - Lower-cased trimmed string form for robust comparison.
    """
    return str(x).strip().lower()


def _is_effective_answer(answer: Any, inactive_values: Set[str]) -> bool:
    """
    Input:
    - answer: `new_answer` from proposed update.
    - inactive_values: placeholder values that mean "not triggered".
    Output:
    - True when answer should be counted as an effective trigger.
    """
    if answer is None:
        return False
    if isinstance(answer, list):
        if not answer:
            return False
        cleaned = [_normalize_str(x) for x in answer if _normalize_str(x)]
        if not cleaned:
            return False
        return not all(item in inactive_values for item in cleaned)
    s = _normalize_str(answer)
    if not s:
        return False
    return s not in inactive_values


def _find_observation_root(trace_run_dir: Path) -> Path:
    """
    Input:
    - trace_run_dir: one run directory under traces/.
    Output:
    - observation root path.
    Raises:
    - FileNotFoundError if neither expected folder exists.
    """
    candidates = [
        trace_run_dir / "observations",
        trace_run_dir / "questionnaire2_observations",
    ]
    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    raise FileNotFoundError(
        f"No observation folder found under: {trace_run_dir}. "
        "Expected one of: observations, questionnaire2_observations"
    )


def _iter_json_files(folder: Path) -> Iterable[Path]:
    """
    Input:
    - folder: directory path
    Output:
    - sorted json file paths (empty iterable if folder missing).
    """
    if not folder.exists():
        return []
    return sorted(folder.glob("*.json"))


def _collect_router_rows(router_dir: Path) -> List[Dict[str, Any]]:
    """
    Input:
    - router_dir path
    Output:
    - list of normalized router rows for UI-level report.
    """
    rows: List[Dict[str, Any]] = []
    for path in _iter_json_files(router_dir):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        rows.append(
            {
                "file_name": path.name,
                "state_sig": data.get("state_sig", ""),
                "matched_block_ids": list(data.get("matched_block_ids", []) or []),
                "router_answers_count": len(data.get("router_answers", []) or []),
            }
        )
    return rows


def _collect_fill_rows(
    fill_dir: Path,
    inactive_values: Set[str],
) -> List[Dict[str, Any]]:
    """
    Input:
    - fill_dir path
    - inactive_values set
    Output:
    - list of normalized fill rows with effective trigger extraction.
    """
    rows: List[Dict[str, Any]] = []
    for path in _iter_json_files(fill_dir):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue

        matched_block_ids = list(data.get("matched_block_ids", []) or [])
        block_fill_results = list(data.get("block_fill_results", []) or [])

        effective_blocks: Set[str] = set()
        effective_questions: List[Dict[str, Any]] = []

        for block_res in block_fill_results:
            if not isinstance(block_res, dict):
                continue
            block_id = str(block_res.get("block_id", "")).strip()
            updates = list(block_res.get("proposed_updates", []) or [])
            for upd in updates:
                if not isinstance(upd, dict):
                    continue
                question_id = str(upd.get("question_id", "")).strip()
                new_answer = upd.get("new_answer", None)
                if not _is_effective_answer(new_answer, inactive_values):
                    continue
                if block_id:
                    effective_blocks.add(block_id)
                effective_questions.append(
                    {
                        "block_id": block_id,
                        "question_id": question_id,
                        "new_answer": new_answer,
                        "confidence": upd.get("confidence"),
                    }
                )

        rows.append(
            {
                "file_name": path.name,
                "state_sig": data.get("state_sig", ""),
                "matched_block_ids": matched_block_ids,
                "router_answers_count": len(data.get("router_answers", []) or []),
                "filled_block_ids": sorted(effective_blocks),
                "effective_questions": effective_questions,
                "effective_question_count": len(effective_questions),
            }
        )
    return rows


def _build_report(
    router_rows: List[Dict[str, Any]],
    fill_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Input:
    - router_rows: normalized router observations
    - fill_rows: normalized fill observations
    Output:
    - report dict with `app_level` and `ui_level`.
    """
    ui_level: List[Dict[str, Any]] = []

    # Primary UI records: use fill rows because they include both routing and fill results.
    for row in fill_rows:
        ui_level.append(
            {
                "ui_file": row["file_name"],
                "state_sig": row["state_sig"],
                "hit_block_ids": row["matched_block_ids"],
                "filled_block_ids": row["filled_block_ids"],
                "effective_questions": row["effective_questions"],
                "effective_question_count": row["effective_question_count"],
                "has_block_hit": len(row["matched_block_ids"]) > 0,
                "has_effective_fill": row["effective_question_count"] > 0,
                "source": "blocks_fill",
            }
        )

    # Add router-only rows for states that do not appear in fill rows.
    fill_state_sigs = {str(r.get("state_sig", "")) for r in fill_rows}
    for row in router_rows:
        state_sig = str(row.get("state_sig", ""))
        if state_sig in fill_state_sigs:
            continue
        ui_level.append(
            {
                "ui_file": row["file_name"],
                "state_sig": state_sig,
                "hit_block_ids": row["matched_block_ids"],
                "filled_block_ids": [],
                "effective_questions": [],
                "effective_question_count": 0,
                "has_block_hit": len(row["matched_block_ids"]) > 0,
                "has_effective_fill": False,
                "source": "router_only",
            }
        )

    app_hit_blocks: Set[str] = set()
    app_filled_blocks: Set[str] = set()
    app_triggered_questions: Set[Tuple[str, str]] = set()
    for ui in ui_level:
        app_hit_blocks.update(ui.get("hit_block_ids", []))
        app_filled_blocks.update(ui.get("filled_block_ids", []))
        for q in ui.get("effective_questions", []):
            block_id = str(q.get("block_id", ""))
            question_id = str(q.get("question_id", ""))
            if block_id and question_id:
                app_triggered_questions.add((block_id, question_id))

    app_level = {
        "total_ui_records": len(ui_level),
        "ui_with_block_hit_count": sum(1 for ui in ui_level if ui["has_block_hit"]),
        "ui_with_effective_fill_count": sum(1 for ui in ui_level if ui["has_effective_fill"]),
        "hit_block_ids_union": sorted(app_hit_blocks),
        "filled_block_ids_union": sorted(app_filled_blocks),
        "triggered_questions_union": [
            {"block_id": b, "question_id": q}
            for (b, q) in sorted(app_triggered_questions)
        ],
    }

    return {"app_level": app_level, "ui_level": ui_level}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize questionnaire observation results (UI-level + APP-level)."
    )
    parser.add_argument(
        "--trace-run-dir",
        required=True,
        help="Path to one trace run directory under traces/",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output JSON path (default: <trace-run-dir>/observations_analysis.json)",
    )
    parser.add_argument(
        "--inactive-values",
        default="",
        help=(
            "Comma-separated placeholder answers treated as not-triggered, "
            "e.g. 'no,none,unknown'."
        ),
    )
    args = parser.parse_args()

    trace_run_dir = Path(args.trace_run_dir).resolve()
    obs_root = _find_observation_root(trace_run_dir)
    router_dir = obs_root / "router"
    fill_dir = obs_root / "blocks_fill"

    inactive_values = set(DEFAULT_INACTIVE_VALUES)
    if args.inactive_values.strip():
        extra = {_normalize_str(x) for x in args.inactive_values.split(",")}
        inactive_values.update(x for x in extra if x)

    router_rows = _collect_router_rows(router_dir)
    fill_rows = _collect_fill_rows(fill_dir, inactive_values)
    report = _build_report(router_rows, fill_rows)

    output_path = (
        Path(args.output).resolve()
        if args.output.strip()
        else trace_run_dir / "observations_analysis.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    app = report["app_level"]
    print(f"[OK] observation root: {obs_root}")
    print(f"[OK] output: {output_path}")
    print(f"[APP] total_ui_records={app['total_ui_records']}")
    print(f"[APP] ui_with_block_hit_count={app['ui_with_block_hit_count']}")
    print(f"[APP] ui_with_effective_fill_count={app['ui_with_effective_fill_count']}")
    print(f"[APP] hit_block_ids_union={len(app['hit_block_ids_union'])}")
    print(f"[APP] filled_block_ids_union={len(app['filled_block_ids_union'])}")
    print(f"[APP] triggered_questions_union={len(app['triggered_questions_union'])}")


if __name__ == "__main__":
    main()
