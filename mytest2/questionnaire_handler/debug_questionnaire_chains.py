"""
debug_questionnaire_chains.py

用途：
- 读取问卷 JSON（当前主要用于 questionnaire-v2/games）。
- 按“从 topic root 到叶子问题的条件路径”展开候选 chains。
- 用来观察原始树结构如果直接路径展开，会得到多少条链、每条链包含哪些问题。

适用场景：
- 早期探索“树状问卷是否适合改成链状结构”。
- 对比不同模块的链数量与链长度。

当前定位：
- 这是一个历史调试脚本，偏“路径链分析”。
- 后续真正用于 router + blocks 执行视图的，不是这个脚本，而是
  debug_split_questionnaire_subtrees.py。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUESTIONNAIRE_DIR = PROJECT_ROOT / "questionnaire-v2" / "games"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "mytest2" / "questionnaire_handler" / "chain_debug"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Debug helper: expand questionnaire trees into candidate linear chains.",
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
        help="Directory to write debug JSON outputs.",
    )
    parser.add_argument(
        "--print-limit",
        type=int,
        default=40,
        help="How many chains to print to console.",
    )
    return parser.parse_args()


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _trigger_label(trigger: Dict[str, Any], parent_node: Dict[str, Any]) -> str:
    ttype = str(trigger.get("type") or "").strip()
    parent_question = str(parent_node.get("question") or parent_node.get("id") or "").strip()
    parent_opts = {
        str(opt.get("id")): str(opt.get("value") or opt.get("id") or "").strip()
        for opt in (parent_node.get("options") or [])
        if isinstance(opt, dict)
    }

    if ttype == "equals":
        answer_id = str(trigger.get("answer_id") or "").strip()
        answer_value = str(trigger.get("answer_value") or "").strip()
        value = parent_opts.get(answer_id) or answer_value or answer_id or "?"
        return f"{parent_question} == {value}"

    if ttype == "contains":
        option_id = str(trigger.get("option_id") or "").strip()
        option_value = str(trigger.get("option_value") or "").strip()
        value = parent_opts.get(option_id) or option_value or option_id or "?"
        return f"{parent_question} contains {value}"

    return f"{parent_question} [unknown trigger]"


def _question_brief(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(node.get("id") or ""),
        "type": str(node.get("type") or "single"),
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


def _build_chain_title(topic_title: str, trigger_labels: List[str], leaf_question: str) -> str:
    parts: List[str] = []
    if topic_title:
        parts.append(topic_title)
    if trigger_labels:
        parts.extend(trigger_labels[-2:])
    if leaf_question and (not parts or leaf_question != parts[-1]):
        parts.append(leaf_question)
    return " -> ".join(part for part in parts if part)


def _emit_chain(
    *,
    namespace: str,
    source_file: str,
    topic_id: str,
    topic_title: str,
    root_id: str,
    path_nodes: List[Dict[str, Any]],
    trigger_labels: List[str],
    chains: List[Dict[str, Any]],
) -> None:
    if not path_nodes:
        return

    question_ids = [str(node.get("id") or "") for node in path_nodes]
    leaf = path_nodes[-1]
    leaf_question = str(leaf.get("question") or leaf.get("id") or "").strip()
    chain_index = len(chains) + 1
    chain_id = f"{namespace}::{topic_id}::chain_{chain_index:03d}"

    chains.append(
        {
            "chain_id": chain_id,
            "namespace": namespace,
            "source_file": source_file,
            "topic_id": topic_id,
            "topic_title": topic_title,
            "root_id": root_id,
            "leaf_id": str(leaf.get("id") or ""),
            "length": len(path_nodes),
            "question_ids": question_ids,
            "trigger_path": list(trigger_labels),
            "title": _build_chain_title(topic_title, trigger_labels, leaf_question),
            "questions": [_question_brief(node) for node in path_nodes],
        }
    )


def _walk_topic_node(
    *,
    namespace: str,
    source_file: str,
    topic_id: str,
    topic_title: str,
    root_id: str,
    node: Dict[str, Any],
    path_nodes: List[Dict[str, Any]],
    trigger_labels: List[str],
    chains: List[Dict[str, Any]],
) -> None:
    current_path = path_nodes + [node]
    child_groups = node.get("children") or []
    if not child_groups:
        _emit_chain(
            namespace=namespace,
            source_file=source_file,
            topic_id=topic_id,
            topic_title=topic_title,
            root_id=root_id,
            path_nodes=current_path,
            trigger_labels=trigger_labels,
            chains=chains,
        )
        return

    emitted_child = False
    for group in child_groups:
        trigger = group.get("trigger") or {}
        nodes = group.get("nodes") or []
        label = _trigger_label(trigger, node)
        for child in nodes:
            emitted_child = True
            _walk_topic_node(
                namespace=namespace,
                source_file=source_file,
                topic_id=topic_id,
                topic_title=topic_title,
                root_id=root_id,
                node=child,
                path_nodes=current_path,
                trigger_labels=trigger_labels + [label],
                chains=chains,
            )

    if not emitted_child:
        _emit_chain(
            namespace=namespace,
            source_file=source_file,
            topic_id=topic_id,
            topic_title=topic_title,
            root_id=root_id,
            path_nodes=current_path,
            trigger_labels=trigger_labels,
            chains=chains,
        )


def _build_topic_chains(doc: Dict[str, Any], source_file: str) -> List[Dict[str, Any]]:
    namespace = str(doc.get("namespace") or Path(source_file).stem)
    chains: List[Dict[str, Any]] = []

    for topic in (doc.get("topics") or []):
        topic_local_id = str(topic.get("id") or "")
        topic_id = f"{namespace}::{topic_local_id}" if topic_local_id else namespace
        topic_title = str(topic.get("title") or "").strip()
        root = topic.get("root") or {}
        root_id = str(root.get("id") or "")
        if root:
            _walk_topic_node(
                namespace=namespace,
                source_file=source_file,
                topic_id=topic_id,
                topic_title=topic_title,
                root_id=root_id,
                node=root,
                path_nodes=[],
                trigger_labels=[],
                chains=chains,
            )

    for idx, node in enumerate(doc.get("always") or [], start=1):
        local_id = str(node.get("id") or f"always_{idx}")
        chains.append(
            {
                "chain_id": f"{namespace}::always::{idx:03d}",
                "namespace": namespace,
                "source_file": source_file,
                "topic_id": f"{namespace}::always",
                "topic_title": "always",
                "root_id": local_id,
                "leaf_id": local_id,
                "length": 1,
                "question_ids": [local_id],
                "trigger_path": [],
                "title": str(node.get("question") or local_id).strip(),
                "questions": [_question_brief(node)],
            }
        )

    return chains


def build_chain_debug(questionnaire_dir: Path) -> Dict[str, Any]:
    files = sorted(questionnaire_dir.glob("*.json"))
    docs_summary: List[Dict[str, Any]] = []
    all_chains: List[Dict[str, Any]] = []

    for path in files:
        doc = _load_json(path)
        if not isinstance(doc, dict) or int(doc.get("schema_version") or 0) != 2:
            continue
        chains = _build_topic_chains(doc, path.name)
        all_chains.extend(chains)
        docs_summary.append(
            {
                "file": path.name,
                "namespace": str(doc.get("namespace") or path.stem),
                "chain_count": len(chains),
                "topic_count": len(doc.get("topics") or []),
                "always_count": len(doc.get("always") or []),
            }
        )

    by_namespace: Dict[str, int] = {}
    for chain in all_chains:
        ns = str(chain.get("namespace") or "")
        by_namespace[ns] = by_namespace.get(ns, 0) + 1

    return {
        "questionnaire_dir": str(questionnaire_dir),
        "summary": {
            "file_count": len(docs_summary),
            "chain_count": len(all_chains),
            "chains_by_namespace": by_namespace,
        },
        "files": docs_summary,
        "chains": all_chains,
    }


def _print_console_summary(payload: Dict[str, Any], print_limit: int) -> None:
    summary = payload.get("summary") or {}
    print("=== questionnaire chain debug ===")
    print(f"questionnaire_dir: {payload.get('questionnaire_dir')}")
    print(f"file_count: {summary.get('file_count')}")
    print(f"chain_count: {summary.get('chain_count')}")
    print("chains_by_namespace:")
    for namespace, count in sorted((summary.get("chains_by_namespace") or {}).items()):
        print(f"  - {namespace}: {count}")

    print("\nfiles:")
    for item in payload.get("files") or []:
        print(
            f"  - {item.get('file')}: namespace={item.get('namespace')} "
            f"topics={item.get('topic_count')} always={item.get('always_count')} "
            f"chains={item.get('chain_count')}"
        )

    print("\nsample_chains:")
    for chain in (payload.get("chains") or [])[: max(0, int(print_limit))]:
        trigger_blob = " | ".join(chain.get("trigger_path") or [])
        print(
            f"  - {chain.get('chain_id')} len={chain.get('length')} "
            f"leaf={chain.get('leaf_id')} title={chain.get('title')}"
        )
        if trigger_blob:
            print(f"    triggers: {trigger_blob}")
        print(f"    question_ids: {', '.join(chain.get('question_ids') or [])}")


def main() -> int:
    args = parse_args()
    payload = build_chain_debug(args.questionnaire_dir)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "questionnaire_chains.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_console_summary(payload, print_limit=args.print_limit)
    print(f"\nwritten: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
