"""
render_split_subtrees_report.py

用途：
- 把 questionnaire_split_subtrees.json 渲染成更容易阅读的 Markdown 报告。
- 便于按模块查看 router、blocks、每个 block 里有哪些问题。

适用场景：
- 你不想直接看 JSON，想看更易读的文本报告时运行。

当前定位：
- 这是 debug_split_questionnaire_subtrees.py 的配套可视化脚本。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "mytest2" / "questionnaire_handler" / "chain_debug" / "questionnaire_split_subtrees.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "mytest2" / "questionnaire_handler" / "chain_debug" / "questionnaire_split_subtrees_report.md"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render split questionnaire subtree JSON into a human-friendly Markdown report.",
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input JSON path.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output Markdown path.")
    return parser.parse_args()


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _fmt_options(options: List[Dict[str, Any]]) -> str:
    vals = [str(opt.get("value") or opt.get("id") or "").strip() for opt in (options or [])]
    vals = [v for v in vals if v]
    if not vals:
        return "-"
    if len(vals) <= 4:
        return " / ".join(vals)
    return " / ".join(vals[:4]) + f" / ... ({len(vals)} options)"


def _render_summary(payload: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    lines.append("# Questionnaire Split Subtrees Report")
    lines.append("")
    lines.append(f"- Source: `{payload.get('questionnaire_dir')}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Namespace | Router Questions | Subtrees | Total Questions | Range | Deferred |")
    lines.append("| --- | ---: | ---: | ---: | --- | --- |")
    for row in (payload.get("summary") or []):
        rng = f"{row.get('question_count_min')}..{row.get('question_count_max')}"
        deferred = str(row.get("deferred_reason") or "-")
        lines.append(
            f"| `{row.get('namespace')}` | {row.get('router_question_count')} | {row.get('subtree_count')} | "
            f"{row.get('question_count_total')} | {rng} | {deferred} |"
        )
    lines.append("")
    return lines


def _render_router_questions(router_questions: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    lines.append("### Router Questions")
    lines.append("")
    if not router_questions:
        lines.append("- None")
        lines.append("")
        return lines

    for rq in router_questions:
        lines.append(f"- `{rq.get('question_id')}`: {rq.get('question')}")
        opts = rq.get("options") or []
        if opts:
            for opt in opts:
                lines.append(f"  - `{opt.get('id')}` -> {opt.get('value')}")
    lines.append("")
    return lines


def _render_subtrees(subtrees: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    lines.append("### Subtrees")
    lines.append("")
    if not subtrees:
        lines.append("- None")
        lines.append("")
        return lines

    for idx, subtree in enumerate(subtrees, start=1):
        qids = subtree.get("question_ids") or []
        lines.append(
            f"#### {idx}. `{subtree.get('subtree_id')}` "
            f"({len(qids)} questions)"
        )
        lines.append("")
        lines.append(f"- Entry kind: `{subtree.get('entry_kind')}`")
        lines.append(f"- Entry label: {subtree.get('entry_label')}")
        if subtree.get("parent_split_node"):
            lines.append(f"- Parent split node: `{subtree.get('parent_split_node')}`")
        trigger = subtree.get("split_trigger")
        if trigger:
            lines.append(f"- Split trigger: `{json.dumps(trigger, ensure_ascii=False)}`")
        lines.append(f"- Question IDs: `{', '.join(qids) if qids else '-'}`")
        lines.append("")
        lines.append("| # | Question ID | Type | Question | Options |")
        lines.append("| ---: | --- | --- | --- | --- |")
        for q_index, q in enumerate((subtree.get("questions") or []), start=1):
            lines.append(
                f"| {q_index} | `{q.get('id')}` | `{q.get('type')}` | "
                f"{str(q.get('question') or '').replace('|', '\\|')} | "
                f"{_fmt_options(q.get('options') or []).replace('|', '\\|')} |"
            )
        lines.append("")
    return lines


def render_report(payload: Dict[str, Any]) -> str:
    lines = _render_summary(payload)
    for namespace_payload in (payload.get("namespaces") or []):
        namespace = str(namespace_payload.get("namespace") or "")
        lines.append(f"## {namespace}")
        lines.append("")
        lines.append(f"- Source file: `{namespace_payload.get('source_file')}`")
        if namespace_payload.get("split_nodes_requested"):
            lines.append(
                "- Requested split nodes: "
                + ", ".join(f"`{node}`" for node in (namespace_payload.get("split_nodes_requested") or []))
            )
        else:
            lines.append("- Requested split nodes: none")
        if namespace_payload.get("split_nodes_effective"):
            lines.append(
                "- Effective split nodes: "
                + ", ".join(f"`{node}`" for node in (namespace_payload.get("split_nodes_effective") or []))
            )
        if namespace_payload.get("deferred_reason"):
            lines.append(f"- Deferred: `{namespace_payload.get('deferred_reason')}`")
        notes = namespace_payload.get("notes") or []
        if notes:
            lines.append("- Notes:")
            for note in notes:
                lines.append(f"  - {note}")
        lines.append("")
        lines.extend(_render_router_questions(namespace_payload.get("router_questions") or []))
        lines.extend(_render_subtrees(namespace_payload.get("subtrees") or []))
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    payload = _load_json(args.input)
    report = render_report(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
