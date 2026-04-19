"""
debug_split_questionnaire_subtrees.py

用途：
- 按指定的 split 节点，把原始问卷树拆成执行视图里的 router + blocks。
- 输出每个模块有哪些 router 问题、有哪些 blocks、每个 block 包含哪些 question_ids。
- 同时导出：
  1. questionnaire_split_subtrees.json   总调试结果
  2. questionnaire_routers.json          仅 router 数据
  3. questionnaire_blocks.json           仅 block 数据

适用场景：
- 当前最重要的问卷拆分调试脚本。
- 当你想验证“某个模块拆成了哪些 blocks、router 是什么”，运行这个脚本。

当前定位：
- 这是后续接入框架前的数据准备/验证脚本。
- 目前 games 模块的拆分规则主要维护在这里。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUESTIONNAIRE_ROOT = PROJECT_ROOT / "questionnaire-v2"
DEFAULT_QUESTIONNAIRE_DIR = DEFAULT_QUESTIONNAIRE_ROOT / "games"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "mytest2" / "questionnaire_handler" / "chain_debug"


# Split configuration grouped by questionnaire collection to avoid namespace collisions.
# Structure:
# {
#   "<collection_name>": {
#       "split_nodes": {"<namespace>": ["node_id", ...] | None},
#       "ignored_namespaces": ["namespace", ...],
#       "ignored_question_ids": {"<namespace>": ["question_id", ...]},
#   }
# }
SPLIT_CONFIG: Dict[str, Dict[str, Any]] = {
    "games": {
        "split_nodes": {
            "misc": None,
            "controlled": ["controlled_substance_includes"],
            "crude": None,
            "fear": ["scary_elements_includes"],
            "violence": ["violence_content"],
            "sexuality": ["sexuality_includes", "revealing_outfits_nudity_includes", "nudity_includes"],
            "language": ["language_includes"],
            "gambling": ["gambling_includes"],
            "digital": ["digital_purchases_includes"],
        },
        "ignored_namespaces": [],
        "ignored_question_ids": {},
    },
    "social_apps": {
        "split_nodes": {
            "social_app": None,
        },
        "ignored_namespaces": [],
        "ignored_question_ids": {},
    },
    "others": {
        "split_nodes": {
            "down": [
                "violence_content",
                "scary_elements_includes",
                "sexuality_includes",
                "revealing_outfits_nudity_includes",
                "nudity_includes",
                "gambling_includes",
                "language_includes",
                "controlled_substance_includes",
            ],
            "miscell": ["misc_rewards_includes"],
            "promotion": None,
            "user": None,
        },
        "ignored_namespaces": ["online"],
        "ignored_question_ids": {
            "down": ["downloaded_content"],
        },
    },
}

DEFERRED_NAMESPACES: Dict[str, str] = {}


@dataclass
class SubtreeSpec:
    subtree_id: str
    block_name: str
    namespace: str
    source_file: str
    topic_id: str
    topic_title: str
    entry_kind: str
    entry_label: str
    question_ids: List[str]
    questions: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]
    parent_split_node: Optional[str]
    split_trigger: Optional[Dict[str, Any]]
    router_path: List[Dict[str, Any]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split questionnaire trees into router + blocks and export JSON outputs.",
    )
    parser.add_argument(
        "--questionnaire-dir",
        type=Path,
        default=None,
        help="Single questionnaire collection directory, e.g. questionnaire-v2/games.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Base directory to write debug JSON outputs.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all configured questionnaire collections (games/social_apps/others). Default when --questionnaire-dir is omitted.",
    )
    return parser.parse_args()


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _option_lookup(node: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for opt in (node.get("options") or []):
        if isinstance(opt, dict):
            key = str(opt.get("id") or "").strip()
            val = str(opt.get("value") or opt.get("id") or "").strip()
            if key:
                out[key] = val
    return out


def _slugify(text: str, max_len: int = 48) -> str:
    s = str(text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return (s or "block")[:max_len]


def _trigger_to_label(trigger: Dict[str, Any], parent_node: Dict[str, Any]) -> str:
    parent_question = str(parent_node.get("question") or parent_node.get("id") or "").strip()
    opts = _option_lookup(parent_node)
    ttype = str(trigger.get("type") or "").strip()
    if ttype == "equals":
        answer_id = str(trigger.get("answer_id") or "").strip()
        answer_value = str(trigger.get("answer_value") or "").strip()
        value = opts.get(answer_id) or answer_value or answer_id or "?"
        return f"{parent_question} == {value}"
    if ttype == "contains":
        option_id = str(trigger.get("option_id") or "").strip()
        option_value = str(trigger.get("option_value") or "").strip()
        value = opts.get(option_id) or option_value or option_id or "?"
        return f"{parent_question} contains {value}"
    return parent_question or "trigger"


def _collect_question_ids(node: Dict[str, Any]) -> List[str]:
    out: List[str] = []

    def walk(cur: Dict[str, Any]) -> None:
        qid = str(cur.get("id") or "").strip()
        if qid:
            out.append(qid)
        for group in (cur.get("children") or []):
            for child in (group.get("nodes") or []):
                walk(child)

    walk(node)
    return out


def _collect_question_ids_from_nodes(nodes: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for node in nodes:
        out.extend(_collect_question_ids(node))
    return out


def _collect_questions(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(cur: Dict[str, Any]) -> None:
        out.append(
            {
                "id": str(cur.get("id") or ""),
                "type": str(cur.get("type") or "single"),
                "question": str(cur.get("question") or "").strip(),
                "options": [
                    {
                        "id": str(opt.get("id") or ""),
                        "value": str(opt.get("value") or "").strip(),
                    }
                    for opt in (cur.get("options") or [])
                    if isinstance(opt, dict)
                ],
            }
        )
        for group in (cur.get("children") or []):
            for child in (group.get("nodes") or []):
                walk(child)

    walk(node)
    return out


def _collect_questions_from_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for node in nodes:
        out.extend(_collect_questions(node))
    return out


def _collect_edges(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(cur: Dict[str, Any]) -> None:
        src = str(cur.get("id") or "").strip()
        for group in (cur.get("children") or []):
            trigger = copy.deepcopy(group.get("trigger") or {})
            for child in (group.get("nodes") or []):
                dst = str(child.get("id") or "").strip()
                if src and dst:
                    out.append(
                        {
                            "source": src,
                            "target": dst,
                            "label": _trigger_to_label(trigger, cur),
                        }
                    )
                walk(child)

    walk(node)
    return out


def _collect_edges_from_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for node in nodes:
        out.extend(_collect_edges(node))
    return out


def _topic_metadata(doc: Dict[str, Any], topic_local_id: str) -> Tuple[str, str]:
    namespace = str(doc.get("namespace") or "")
    for topic in (doc.get("topics") or []):
        if str(topic.get("id") or "") == topic_local_id:
            return f"{namespace}::{topic_local_id}", str(topic.get("title") or "").strip()
    return f"{namespace}::{topic_local_id}", topic_local_id


def _split_children_as_subtrees(
    *,
    namespace: str,
    source_file: str,
    topic_id: str,
    topic_title: str,
    split_node: Dict[str, Any],
    router_path: Optional[List[Dict[str, Any]]] = None,
) -> List[SubtreeSpec]:
    subtrees: List[SubtreeSpec] = []
    split_node_id = str(split_node.get("id") or "")
    seq = 0

    for group in (split_node.get("children") or []):
        trigger = copy.deepcopy(group.get("trigger") or {})
        label = _trigger_to_label(trigger, split_node)
        child_nodes = list(group.get("nodes") or [])
        if not child_nodes:
            continue
        seq += 1
        qids = _collect_question_ids_from_nodes(child_nodes)
        option_token = str(trigger.get("option_id") or trigger.get("option_value") or f"branch_{seq}").strip()
        subtrees.append(
            SubtreeSpec(
                subtree_id=f"{namespace}::{split_node_id}::subtree_{seq:03d}",
                block_name=f"{namespace}.{_slugify(split_node_id)}.{_slugify(option_token)}",
                namespace=namespace,
                source_file=source_file,
                topic_id=topic_id,
                topic_title=topic_title,
                entry_kind="split_option_branch",
                entry_label=label,
                question_ids=qids,
                questions=_collect_questions_from_nodes(child_nodes),
                edges=_collect_edges_from_nodes(child_nodes),
                parent_split_node=split_node_id,
                split_trigger=trigger,
                router_path=list(router_path or []),
            )
        )
    return subtrees


def _keep_whole_tree(
    *,
    namespace: str,
    source_file: str,
    topic_id: str,
    topic_title: str,
    node: Dict[str, Any],
    entry_kind: str,
    entry_label: str,
) -> SubtreeSpec:
    root_id = str(node.get("id") or "")
    return SubtreeSpec(
        subtree_id=f"{namespace}::{root_id or entry_kind}",
        block_name=f"{namespace}.{_slugify(root_id or entry_kind)}",
        namespace=namespace,
        source_file=source_file,
        topic_id=topic_id,
        topic_title=topic_title,
        entry_kind=entry_kind,
        entry_label=entry_label,
        question_ids=_collect_question_ids(node),
        questions=_collect_questions(node),
        edges=_collect_edges(node),
        parent_split_node=None,
        split_trigger=None,
        router_path=[],
    )


def _filtered_node_copy(node: Dict[str, Any], ignored_question_ids: set[str]) -> Optional[Dict[str, Any]]:
    qid = str(node.get("id") or "").strip()
    if qid and qid in ignored_question_ids:
        return None

    new_node = copy.deepcopy(node)
    new_children: List[Dict[str, Any]] = []
    for group in (node.get("children") or []):
        kept_nodes: List[Dict[str, Any]] = []
        for child in (group.get("nodes") or []):
            filtered = _filtered_node_copy(child, ignored_question_ids)
            if filtered is not None:
                kept_nodes.append(filtered)
        if kept_nodes:
            new_group = copy.deepcopy(group)
            new_group["nodes"] = kept_nodes
            new_children.append(new_group)
    new_node["children"] = new_children
    return new_node


def _find_topic_root(doc: Dict[str, Any], split_node_id: str) -> Optional[Dict[str, Any]]:
    for topic in (doc.get("topics") or []):
        root = topic.get("root") or {}
        if _contains_node(root, split_node_id):
            return topic
    return None


def _contains_node(node: Dict[str, Any], target_id: str) -> bool:
    if str(node.get("id") or "") == target_id:
        return True
    for group in (node.get("children") or []):
        for child in (group.get("nodes") or []):
            if _contains_node(child, target_id):
                return True
    return False


def _find_node(node: Dict[str, Any], target_id: str) -> Optional[Dict[str, Any]]:
    if str(node.get("id") or "") == target_id:
        return node
    for group in (node.get("children") or []):
        for child in (group.get("nodes") or []):
            found = _find_node(child, target_id)
            if found is not None:
                return found
    return None


def _build_namespace_payload(path: Path, doc: Dict[str, Any]) -> Dict[str, Any]:
    namespace = str(doc.get("namespace") or path.stem)
    collection_name = path.parent.name
    collection_cfg = SPLIT_CONFIG.get(collection_name, {})
    split_nodes = (collection_cfg.get("split_nodes") or {}).get(namespace)
    ignored_namespaces = set(collection_cfg.get("ignored_namespaces") or [])
    ignored_question_ids = set(((collection_cfg.get("ignored_question_ids") or {}).get(namespace)) or [])
    deferred_reason = DEFERRED_NAMESPACES.get(namespace)

    if namespace in ignored_namespaces:
        return {
            "namespace": namespace,
            "source_file": path.name,
            "split_nodes_requested": [],
            "deferred_reason": "ignored_namespace",
            "router_questions": [],
            "router_edges": [],
            "subtrees": [],
            "notes": ["namespace ignored by split config"],
            "split_nodes_effective": [],
        }

    payload: Dict[str, Any] = {
        "namespace": namespace,
        "source_file": path.name,
        "split_nodes_requested": split_nodes or [],
        "deferred_reason": deferred_reason,
        "router_questions": [],
        "router_edges": [],
        "subtrees": [],
        "notes": [],
    }

    if deferred_reason:
        payload["notes"].append("namespace deferred; original structure preserved for now")

    if split_nodes is None or deferred_reason:
        for topic in (doc.get("topics") or []):
            root = _filtered_node_copy(topic.get("root") or {}, ignored_question_ids)
            if not root:
                continue
            topic_local_id = str(topic.get("id") or "")
            topic_id, topic_title = _topic_metadata(doc, topic_local_id)
            payload["subtrees"].append(
                _keep_whole_tree(
                    namespace=namespace,
                    source_file=path.name,
                    topic_id=topic_id,
                    topic_title=topic_title,
                    node=root,
                    entry_kind="topic_root",
                    entry_label=str(root.get("question") or root.get("id") or "").strip(),
                ).__dict__
            )

        for idx, node in enumerate((doc.get("always") or []), start=1):
            if str(node.get("id") or "") in ignored_question_ids:
                continue
            payload["subtrees"].append(
                _keep_whole_tree(
                    namespace=namespace,
                    source_file=path.name,
                    topic_id=f"{namespace}::always",
                    topic_title="always",
                    node=node,
                    entry_kind="always",
                    entry_label=str(node.get("question") or node.get("id") or f"always_{idx}").strip(),
                ).__dict__
            )
        return payload

    if namespace == "sexuality":
        _build_sexuality_payload(payload, path, doc)
        return payload

    seen_subtree_keys: set[Tuple[str, str]] = set()
    found_split_nodes: List[str] = []
    handled_topic_ids: set[str] = set()

    for split_node_id in split_nodes:
        topic = _find_topic_root(doc, split_node_id)
        if topic is None:
            payload["notes"].append(f"split node not found: {split_node_id}")
            continue

        root = topic.get("root") or {}
        split_node = _find_node(root, split_node_id)
        if split_node is None:
            payload["notes"].append(f"split node not found under topic root: {split_node_id}")
            continue

        found_split_nodes.append(split_node_id)
        topic_local_id = str(topic.get("id") or "")
        topic_id, topic_title = _topic_metadata(doc, topic_local_id)
        handled_topic_ids.add(topic_id)
        payload["router_questions"].append(
            {
                "question_id": split_node_id,
                "router_name": f"{namespace}.{_slugify(split_node_id)}",
                "topic_id": topic_id,
                "question": str(split_node.get("question") or "").strip(),
                "type": str(split_node.get("type") or "single"),
                "options": [
                    {
                        "id": str(opt.get("id") or ""),
                        "value": str(opt.get("value") or "").strip(),
                    }
                    for opt in (split_node.get("options") or [])
                    if isinstance(opt, dict)
                ],
            }
        )

        subtrees = _split_children_as_subtrees(
            namespace=namespace,
            source_file=path.name,
            topic_id=topic_id,
            topic_title=topic_title,
            split_node=split_node,
            router_path=[{"question_id": split_node_id}],
        )
        for subtree in subtrees:
            key = (subtree.parent_split_node or "", subtree.entry_label)
            if key in seen_subtree_keys:
                continue
            seen_subtree_keys.add(key)
            payload["subtrees"].append(subtree.__dict__)

    payload["split_nodes_effective"] = found_split_nodes

    # Keep unsplit topic roots as direct blocks.
    for topic in (doc.get("topics") or []):
        root = _filtered_node_copy(topic.get("root") or {}, ignored_question_ids)
        if not root:
            continue
        topic_local_id = str(topic.get("id") or "")
        topic_id, topic_title = _topic_metadata(doc, topic_local_id)
        if topic_id in handled_topic_ids:
            continue
        payload["subtrees"].append(
            _keep_whole_tree(
                namespace=namespace,
                source_file=path.name,
                topic_id=topic_id,
                topic_title=topic_title,
                node=root,
                entry_kind="topic_root",
                entry_label=str(root.get("question") or root.get("id") or "").strip(),
            ).__dict__
        )

    # Keep always questions as direct blocks.
    for idx, node in enumerate((doc.get("always") or []), start=1):
        if str(node.get("id") or "") in ignored_question_ids:
            continue
        payload["subtrees"].append(
            _keep_whole_tree(
                namespace=namespace,
                source_file=path.name,
                topic_id=f"{namespace}::always",
                topic_title="always",
                node=node,
                entry_kind="always",
                entry_label=str(node.get("question") or node.get("id") or f"always_{idx}").strip(),
            ).__dict__
        )

    return payload


def _register_router_question(payload: Dict[str, Any], *, topic_id: str, node: Dict[str, Any]) -> None:
    qid = str(node.get("id") or "").strip()
    if not qid:
        return
    if any(str(item.get("question_id") or "") == qid for item in (payload.get("router_questions") or [])):
        return
    payload["router_questions"].append(
        {
            "question_id": qid,
            "router_name": f"{str(payload.get('namespace') or '')}.{_slugify(qid)}",
            "topic_id": topic_id,
            "question": str(node.get("question") or "").strip(),
            "type": str(node.get("type") or "single"),
            "options": [
                {
                    "id": str(opt.get("id") or ""),
                    "value": str(opt.get("value") or "").strip(),
                }
                for opt in (node.get("options") or [])
                if isinstance(opt, dict)
            ],
        }
    )


def _build_sexuality_payload(payload: Dict[str, Any], path: Path, doc: Dict[str, Any]) -> None:
    namespace = str(doc.get("namespace") or path.stem)
    topic = (doc.get("topics") or [None])[0]
    if not topic:
        payload["notes"].append("sexuality topic missing")
        return
    root = topic.get("root") or {}
    topic_local_id = str(topic.get("id") or "")
    topic_id, topic_title = _topic_metadata(doc, topic_local_id)

    sexuality_content = root
    sexuality_includes = _find_node(root, "sexuality_includes")
    revealing = _find_node(root, "revealing_outfits_nudity_includes")
    nudity = _find_node(root, "nudity_includes")
    nudity_erotic = _find_node(root, "nudity_erotic_context")

    for node in [sexuality_includes, revealing, nudity]:
        if node:
            _register_router_question(payload, topic_id=topic_id, node=node)

    payload["split_nodes_effective"] = [
        q["question_id"] for q in (payload.get("router_questions") or [])
    ]

    payload["router_edges"] = [
        {"source": "sexuality_includes", "target": "revealing_outfits_nudity_includes", "label": "contains nudity_or_revealing_outfits"},
        {"source": "revealing_outfits_nudity_includes", "target": "nudity_includes", "label": "contains nudity"},
    ]

    if not sexuality_includes or not revealing or not nudity:
        payload["notes"].append("sexuality router nodes not fully found")
        return

    # Top-level sexuality branches other than nudity/revealing outfits.
    top_level_router_path = [{"question_id": "sexuality_includes"}]
    payload["subtrees"].extend(
        subtree.__dict__
        for subtree in _split_children_as_subtrees(
            namespace=namespace,
            source_file=path.name,
            topic_id=topic_id,
            topic_title=topic_title,
            split_node=sexuality_includes,
            router_path=top_level_router_path,
        )
        if str((subtree.split_trigger or {}).get("option_id") or "") != "nudity_or_revealing_outfits"
    )

    # Revealing outfits branch.
    revealing_router_path = [
        {"question_id": "sexuality_includes", "trigger_option_id": "nudity_or_revealing_outfits"},
        {"question_id": "revealing_outfits_nudity_includes"},
    ]
    for subtree in _split_children_as_subtrees(
        namespace=namespace,
        source_file=path.name,
        topic_id=topic_id,
        topic_title=topic_title,
        split_node=revealing,
        router_path=revealing_router_path,
    ):
        option_id = str((subtree.split_trigger or {}).get("option_id") or "")
        if option_id == "revealing_outfits":
            payload["subtrees"].append(subtree.__dict__)

    # Nudity common subtree: applies once nudity branch is selected, before specific nudity types.
    if nudity_erotic:
        payload["subtrees"].append(
            SubtreeSpec(
                subtree_id=f"{namespace}::nudity_common",
                block_name=f"{namespace}.revealing_outfits_nudity_includes.nudity_common",
                namespace=namespace,
                source_file=path.name,
                topic_id=topic_id,
                topic_title=topic_title,
                entry_kind="router_followup",
                entry_label="revealing_outfits_nudity_includes contains Nudity -> common followup",
                question_ids=_collect_question_ids(nudity_erotic),
                questions=_collect_questions(nudity_erotic),
                edges=_collect_edges(nudity_erotic),
                parent_split_node="revealing_outfits_nudity_includes",
                split_trigger={"type": "contains", "option_id": "nudity", "option_value": "Nudity"},
                router_path=[
                    {"question_id": "sexuality_includes", "trigger_option_id": "nudity_or_revealing_outfits"},
                    {"question_id": "revealing_outfits_nudity_includes", "trigger_option_id": "nudity"},
                ],
            ).__dict__
        )

    # Nudity subtype branches.
    nudity_router_path = [
        {"question_id": "sexuality_includes", "trigger_option_id": "nudity_or_revealing_outfits"},
        {"question_id": "revealing_outfits_nudity_includes", "trigger_option_id": "nudity"},
        {"question_id": "nudity_includes"},
    ]
    payload["subtrees"].extend(
        subtree.__dict__
        for subtree in _split_children_as_subtrees(
            namespace=namespace,
            source_file=path.name,
            topic_id=topic_id,
            topic_title=topic_title,
            split_node=nudity,
            router_path=nudity_router_path,
        )
    )


def build_payload(questionnaire_dir: Path) -> Dict[str, Any]:
    namespaces: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []
    collection_name = questionnaire_dir.name

    for path in sorted(questionnaire_dir.glob("*.json")):
        doc = _load_json(path)
        if not isinstance(doc, dict) or int(doc.get("schema_version") or 0) != 2:
            continue
        namespace_payload = _build_namespace_payload(path, doc)
        namespaces.append(namespace_payload)

        subtree_sizes = [len(st.get("question_ids") or []) for st in (namespace_payload.get("subtrees") or [])]
        summary_rows.append(
            {
                "namespace": namespace_payload.get("namespace"),
                "source_file": namespace_payload.get("source_file"),
                "router_question_count": len(namespace_payload.get("router_questions") or []),
                "subtree_count": len(namespace_payload.get("subtrees") or []),
                "question_count_total": sum(subtree_sizes),
                "question_count_min": min(subtree_sizes) if subtree_sizes else 0,
                "question_count_max": max(subtree_sizes) if subtree_sizes else 0,
                "deferred_reason": namespace_payload.get("deferred_reason"),
            }
        )

    return {
        "questionnaire_dir": str(questionnaire_dir),
        "questionnaire_collection": collection_name,
        "split_config": SPLIT_CONFIG.get(collection_name, {}),
        "deferred_namespaces": DEFERRED_NAMESPACES,
        "summary": summary_rows,
        "namespaces": namespaces,
        "routers": _collect_routers(namespaces),
        "blocks": _collect_blocks(namespaces),
    }


def _collect_routers(namespaces: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for ns in namespaces:
        namespace = str(ns.get("namespace") or "")
        out[namespace] = {
            "namespace": namespace,
            "router_questions": ns.get("router_questions") or [],
            "router_edges": ns.get("router_edges") or [],
        }
    return out


def _collect_blocks(namespaces: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for ns in namespaces:
        namespace = str(ns.get("namespace") or "")
        blocks: List[Dict[str, Any]] = []
        for block in (ns.get("subtrees") or []):
            question_ids = block.get("question_ids") or []
            blocks.append(
                {
                    "block_id": block.get("subtree_id"),
                    "block_name": block.get("block_name"),
                    "entry_kind": block.get("entry_kind"),
                    "entry_label": block.get("entry_label"),
                    "parent_split_node": block.get("parent_split_node"),
                    "split_trigger": block.get("split_trigger"),
                    "router_path": block.get("router_path") or [],
                    "question_ids": question_ids,
                    "question_full_ids": [f"{namespace}.{qid}" for qid in question_ids],
                    "questions": block.get("questions") or [],
                    "edges": block.get("edges") or [],
                }
            )
        out[namespace] = {"namespace": namespace, "blocks": blocks}
    return out


def _print_payload(payload: Dict[str, Any]) -> None:
    print("=== split questionnaire subtrees ===")
    print(f"questionnaire_dir: {payload.get('questionnaire_dir')}")
    print("summary:")
    for row in (payload.get("summary") or []):
        line = (
            f"  - {row.get('namespace')}: router_questions={row.get('router_question_count')} "
            f"subtrees={row.get('subtree_count')} questions_total={row.get('question_count_total')} "
            f"range=[{row.get('question_count_min')}, {row.get('question_count_max')}]"
        )
        if row.get("deferred_reason"):
            line += f" deferred={row.get('deferred_reason')}"
        print(line)


def _write_payload_files(payload: Dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "questionnaire_split_subtrees.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    routers_path = out_dir / "questionnaire_routers.json"
    routers_path.write_text(json.dumps(payload.get("routers") or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    blocks_path = out_dir / "questionnaire_blocks.json"
    blocks_path.write_text(json.dumps(payload.get("blocks") or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten: {out_path}")
    print(f"written: {routers_path}")
    print(f"written: {blocks_path}")


def main() -> int:
    args = parse_args()
    if args.questionnaire_dir is not None:
        payload = build_payload(args.questionnaire_dir)
        _print_payload(payload)
        _write_payload_files(payload, args.out_dir)
        return 0

    collection_names = list(SPLIT_CONFIG.keys())
    for collection_name in collection_names:
        questionnaire_dir = DEFAULT_QUESTIONNAIRE_ROOT / collection_name
        payload = build_payload(questionnaire_dir)
        print(f"\n=== collection: {collection_name} ===")
        _print_payload(payload)
        _write_payload_files(payload, args.out_dir / f"{collection_name}_split")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
