"""
render_split_subtrees_mmd.py

用途：
- 把 questionnaire_split_subtrees.json 渲染成按模块拆分的 Mermaid 图（.mmd）。
- 图里包含 router 和各个 block，节点只显示 question_id，便于快速看结构。

适用场景：
- 当你想直观看“router 如何连到 blocks、每个 block 里有哪些问题”时运行。

当前定位：
- 这是 debug_split_questionnaire_subtrees.py 的配套图形化脚本。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "mytest2" / "questionnaire_handler" / "chain_debug" / "questionnaire_split_subtrees.json"
DEFAULT_OUT_DIR = PROJECT_ROOT / "mytest2" / "questionnaire_handler" / "chain_debug" / "split_subtrees_mmd"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render split subtree JSON as simple Mermaid diagrams with question IDs only.",
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input JSON path.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output directory.")
    return parser.parse_args()


def _safe_id(raw: str) -> str:
    return "n_" + re.sub(r"[^a-zA-Z0-9_]", "_", raw)


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _render_namespace(namespace_payload: Dict[str, Any]) -> str:
    lines: List[str] = ["flowchart TD"]

    router_questions = namespace_payload.get("router_questions") or []
    if router_questions:
        lines.append("  subgraph router[router]")
        for rq in router_questions:
            qid = str(rq.get("question_id") or "")
            nid = _safe_id(f"router_{qid}")
            lines.append(f'    {nid}["{qid}"]')
        for edge in (namespace_payload.get("router_edges") or []):
            src = _safe_id(f"router_{edge.get('source')}")
            dst = _safe_id(f"router_{edge.get('target')}")
            label = str(edge.get("label") or "").replace('"', "'")
            lines.append(f'    {src} -->|"{label}"| {dst}')
        lines.append("  end")

    for idx, subtree in enumerate((namespace_payload.get("subtrees") or []), start=1):
        title = f"{idx:02d}_{subtree.get('subtree_id')}"
        lines.append(f"  subgraph st_{idx}[{title}]")

        qids = [str(q.get("id") or "") for q in (subtree.get("questions") or []) if str(q.get("id") or "")]
        for qid in qids:
            nid = _safe_id(f"st{idx}_{qid}")
            lines.append(f'    {nid}["{qid}"]')

        edges = subtree.get("edges") or []
        if edges:
            for edge in edges:
                src = _safe_id(f"st{idx}_{edge.get('source')}")
                dst = _safe_id(f"st{idx}_{edge.get('target')}")
                label = str(edge.get("label") or "").replace('"', "'")
                lines.append(f'    {src} -->|"{label}"| {dst}')
        elif qids:
            # Keep single-node subtree visible.
            pass

        lines.append("  end")

        parent_split_node = str(subtree.get("parent_split_node") or "")
        if parent_split_node and qids:
            router_src = _safe_id(f"router_{parent_split_node}")
            subtree_dst = _safe_id(f"st{idx}_{qids[0]}")
            trigger = subtree.get("split_trigger") or {}
            option = str(trigger.get("option_id") or trigger.get("option_value") or subtree.get("entry_label") or "")
            option = option.replace('"', "'")
            lines.append(f'  {router_src} -.->|"{option}"| {subtree_dst}')

    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    payload = _load_json(args.input)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for namespace_payload in (payload.get("namespaces") or []):
        namespace = str(namespace_payload.get("namespace") or "unknown")
        out_path = args.out_dir / f"{namespace}.mmd"
        out_path.write_text(_render_namespace(namespace_payload), encoding="utf-8")
        print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
