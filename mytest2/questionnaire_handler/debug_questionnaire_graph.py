"""
debug_questionnaire_graph.py

用途：
- 读取问卷 JSON，并导出“问题节点 + 依赖边”的完整树/图结构。
- 生成结构化 JSON，以及按 namespace 拆分的 Mermaid 图（.md / .mmd）。

适用场景：
- 直观看某个模块的原始问卷树长什么样。
- 找出“出边很多、适合作为拆分点”的多选问题。

当前定位：
- 这是观察原始树结构的可视化脚本。
- 当你想看“还没拆分之前的问卷结构”，运行这个脚本。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUESTIONNAIRE_DIR = PROJECT_ROOT / "questionnaire-v2" / "games"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "mytest2" / "questionnaire_handler" / "chain_debug"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export questionnaire dependency graph as Mermaid + JSON for visual debugging.",
    )
    parser.add_argument(
        "--questionnaire-dir",
        type=Path,
        default=DEFAULT_QUESTIONNAIRE_DIR,
        help="Directory containing questionnaire JSON files.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write graph outputs.",
    )
    return parser.parse_args()


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _safe_node_id(raw: str) -> str:
    return "n_" + re.sub(r"[^a-zA-Z0-9_]", "_", raw)


def _short_text(text: str, limit: int = 68) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _trigger_label(trigger: Dict[str, Any], parent_node: Dict[str, Any]) -> str:
    ttype = str(trigger.get("type") or "").strip()
    opts = {
        str(opt.get("id")): str(opt.get("value") or opt.get("id") or "").strip()
        for opt in (parent_node.get("options") or [])
        if isinstance(opt, dict)
    }

    if ttype == "equals":
        answer_id = str(trigger.get("answer_id") or "").strip()
        answer_value = str(trigger.get("answer_value") or "").strip()
        value = opts.get(answer_id) or answer_value or answer_id or "?"
        return f"== {value}"

    if ttype == "contains":
        option_id = str(trigger.get("option_id") or "").strip()
        option_value = str(trigger.get("option_value") or "").strip()
        value = opts.get(option_id) or option_value or option_id or "?"
        return f"contains {value}"

    return "trigger"


def _node_kind(node: Dict[str, Any], is_topic_root: bool = False, is_always: bool = False) -> str:
    if is_always:
        return "always"
    if is_topic_root:
        return "topic_root"
    qtype = str(node.get("type") or "single").lower()
    if qtype == "multiple":
        return "multiple"
    return "single"


def _node_record(
    *,
    namespace: str,
    topic_id: str,
    source_file: str,
    node: Dict[str, Any],
    is_topic_root: bool = False,
    is_always: bool = False,
) -> Dict[str, Any]:
    local_id = str(node.get("id") or "")
    full_id = f"{namespace}.{local_id}"
    return {
        "id": full_id,
        "local_id": local_id,
        "namespace": namespace,
        "topic_id": topic_id,
        "source_file": source_file,
        "kind": _node_kind(node, is_topic_root=is_topic_root, is_always=is_always),
        "type": str(node.get("type") or "single").lower(),
        "question": str(node.get("question") or "").strip(),
        "options": [
            {
                "id": str(opt.get("id") or ""),
                "value": str(opt.get("value") or "").strip(),
            }
            for opt in (node.get("options") or [])
            if isinstance(opt, dict)
        ],
    }


def _walk_topic(
    *,
    namespace: str,
    topic_id: str,
    topic_title: str,
    source_file: str,
    node: Dict[str, Any],
    nodes: Dict[str, Dict[str, Any]],
    edges: List[Dict[str, Any]],
    is_topic_root: bool = False,
) -> None:
    current = _node_record(
        namespace=namespace,
        topic_id=topic_id,
        source_file=source_file,
        node=node,
        is_topic_root=is_topic_root,
    )
    nodes[current["id"]] = current

    for group in (node.get("children") or []):
        trigger = group.get("trigger") or {}
        label = _trigger_label(trigger, node)
        for child in (group.get("nodes") or []):
            child_full_id = f"{namespace}.{child.get('id')}"
            edges.append(
                {
                    "source": current["id"],
                    "target": child_full_id,
                    "label": label,
                    "topic_id": topic_id,
                    "topic_title": topic_title,
                }
            )
            _walk_topic(
                namespace=namespace,
                topic_id=topic_id,
                topic_title=topic_title,
                source_file=source_file,
                node=child,
                nodes=nodes,
                edges=edges,
                is_topic_root=False,
            )


def build_graph_payload(questionnaire_dir: Path) -> Dict[str, Any]:
    graph_by_namespace: Dict[str, Dict[str, Any]] = {}

    for path in sorted(questionnaire_dir.glob("*.json")):
        doc = _load_json(path)
        if not isinstance(doc, dict) or int(doc.get("schema_version") or 0) != 2:
            continue

        namespace = str(doc.get("namespace") or path.stem)
        bucket = graph_by_namespace.setdefault(
            namespace,
            {
                "namespace": namespace,
                "source_file": path.name,
                "topics": [],
                "nodes": {},
                "edges": [],
            },
        )

        for topic in (doc.get("topics") or []):
            topic_local_id = str(topic.get("id") or "")
            topic_id = f"{namespace}::{topic_local_id}"
            topic_title = str(topic.get("title") or "").strip()
            bucket["topics"].append(
                {
                    "topic_id": topic_id,
                    "title": topic_title,
                    "root_id": f"{namespace}.{(topic.get('root') or {}).get('id')}",
                }
            )
            root = topic.get("root") or {}
            if root:
                _walk_topic(
                    namespace=namespace,
                    topic_id=topic_id,
                    topic_title=topic_title,
                    source_file=path.name,
                    node=root,
                    nodes=bucket["nodes"],
                    edges=bucket["edges"],
                    is_topic_root=True,
                )

        for idx, node in enumerate((doc.get("always") or []), start=1):
            rec = _node_record(
                namespace=namespace,
                topic_id=f"{namespace}::always",
                source_file=path.name,
                node=node,
                is_always=True,
            )
            bucket["nodes"][rec["id"]] = rec

    files: List[Dict[str, Any]] = []
    total_nodes = 0
    total_edges = 0
    for namespace, payload in sorted(graph_by_namespace.items()):
        node_count = len(payload["nodes"])
        edge_count = len(payload["edges"])
        total_nodes += node_count
        total_edges += edge_count
        files.append(
            {
                "namespace": namespace,
                "source_file": payload["source_file"],
                "topic_count": len(payload["topics"]),
                "node_count": node_count,
                "edge_count": edge_count,
            }
        )

    return {
        "questionnaire_dir": str(questionnaire_dir),
        "summary": {
            "namespace_count": len(graph_by_namespace),
            "node_count": total_nodes,
            "edge_count": total_edges,
        },
        "files": files,
        "graphs": graph_by_namespace,
    }


def _mermaid_node_line(node: Dict[str, Any]) -> str:
    node_id = _safe_node_id(str(node.get("id") or ""))
    kind = str(node.get("kind") or "")
    qtype = str(node.get("type") or "")
    local_id = str(node.get("local_id") or "")
    question = _short_text(str(node.get("question") or ""))
    label = f"{local_id}<br/>{qtype}<br/>{question}"

    if kind == "topic_root":
        return f'    {node_id}(["{label}"])'
    if kind == "multiple":
        return f'    {node_id}{{"{label}"}}'
    if kind == "always":
        return f'    {node_id}[/"{label}"/]'
    return f'    {node_id}["{label}"]'


def _mermaid_edge_line(edge: Dict[str, Any]) -> str:
    source = _safe_node_id(str(edge.get("source") or ""))
    target = _safe_node_id(str(edge.get("target") or ""))
    label = _short_text(str(edge.get("label") or ""), limit=48).replace('"', "'")
    return f'    {source} -->|"{label}"| {target}'


def _build_mermaid(namespace_payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("flowchart TD")

    for topic in (namespace_payload.get("topics") or []):
        topic_id = str(topic.get("topic_id") or "")
        topic_title = _short_text(str(topic.get("title") or ""), limit=90).replace('"', "'")
        root_id = _safe_node_id(str(topic.get("root_id") or ""))
        topic_node = _safe_node_id(topic_id)
        lines.append(f'    {topic_node}(["{topic_title}"])')
        if root_id != "n_":
            lines.append(f"    {topic_node} --> {root_id}")

    for node in (namespace_payload.get("nodes") or {}).values():
        lines.append(_mermaid_node_line(node))

    for edge in (namespace_payload.get("edges") or []):
        lines.append(_mermaid_edge_line(edge))

    return "\n".join(lines) + "\n"


def _build_markdown(namespace_payload: Dict[str, Any]) -> str:
    namespace = str(namespace_payload.get("namespace") or "")
    lines: List[str] = []
    lines.append(f"# Questionnaire Graph: {namespace}")
    lines.append("")
    lines.append("说明：")
    lines.append("- 圆角节点：topic root")
    lines.append("- 菱形节点：multiple 题")
    lines.append("- 普通矩形：single 题")
    lines.append("- 平行四边形：always 题")
    lines.append("- 边标签：依赖条件")
    lines.append("")
    lines.append("```mermaid")
    lines.append(_build_mermaid(namespace_payload).rstrip())
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _print_summary(payload: Dict[str, Any]) -> None:
    summary = payload.get("summary") or {}
    print("=== questionnaire graph debug ===")
    print(f"questionnaire_dir: {payload.get('questionnaire_dir')}")
    print(f"namespace_count: {summary.get('namespace_count')}")
    print(f"node_count: {summary.get('node_count')}")
    print(f"edge_count: {summary.get('edge_count')}")
    print("files:")
    for item in (payload.get("files") or []):
        print(
            f"  - {item.get('namespace')}: file={item.get('source_file')} "
            f"topics={item.get('topic_count')} nodes={item.get('node_count')} edges={item.get('edge_count')}"
        )


def main() -> int:
    args = parse_args()
    payload = build_graph_payload(args.questionnaire_dir)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "questionnaire_graph.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for namespace, namespace_payload in (payload.get("graphs") or {}).items():
        md_path = out_dir / f"questionnaire_graph_{namespace}.md"
        mmd_path = out_dir / f"questionnaire_graph_{namespace}.mmd"
        md_path.write_text(_build_markdown(namespace_payload), encoding="utf-8")
        mmd_path.write_text(_build_mermaid(namespace_payload), encoding="utf-8")

    _print_summary(payload)
    print(f"\nwritten: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
