"""
migrate_questionnaires_v1_to_v2.py

Migrate your existing questionnaire JSON files (v1) into a more optimal v2 hierarchy.

Why v2 is better:
- Explicit topic / gate / child relationships (instead of scattered show_if).
- Enables code-level inference:
    - child evidence => imply parent gate/option
    - parent yes => prioritize children
- Improves LLM focus by sending compact packs grouped by topic + active gates.

Input v1 format (your current):
{
  "language_content": { "question": "...", "type": "single", "options":[{"value":"Yes"},{"value":"No"}], "show_if": null },
  "language_includes": { "question": "...", "type": "multiple", "options":[...], "show_if": {"id":"language_content","answer":"Yes"} },
  ...
}

Output v2 format:
{
  "schema_version": 2,
  "namespace": "<file_basename>",
  "topics": [
    {
      "id": "<root_gate_local_id>",
      "title": "<root_gate_question>",
      "root": {
        "id": "<root_gate_local_id>",
        "question": "...",
        "type": "single",
        "options": [{"id":"yes","value":"Yes"}, {"id":"no","value":"No"}],
        "children": [
          {
            "trigger": {"type":"equals","answer_id":"yes"},
            "nodes": [
              {
                "id":"language_includes",
                ...
                "children":[ ... ]
              }
            ]
          }
        ]
      }
    }
  ],
  "always": [ ... questions with show_if null ... ]
}

Usage:
  python migrate_questionnaires.py --input-dir questionnaire/games --output-dir questionnaire_v2/games

Notes:
- We keep question ids unchanged.
- We generate stable option ids:
    - for Yes/No => "yes" and "no"
    - otherwise slugify(value), with collision suffixes.
- For v1 show_if:
    - parent single => trigger type "equals"
    - parent multiple => trigger type "contains" (option match)

This script does not depend on your project code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Dict, List, Tuple


def slugify(s: str, max_len: int = 40) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return (s or "opt")[:max_len]


def normalize_options(raw_opts: Any) -> List[Dict[str, Any]]:
    """
    Produce v2 option list: [{"id":..., "value":..., "learn_more":...}, ...]
    """
    out: List[Dict[str, Any]] = []
    vals: List[str] = []
    for o in (raw_opts or []):
        if isinstance(o, dict):
            vals.append(str(o.get("value", "")).strip())
        else:
            vals.append(str(o).strip())

    # Special-case Yes/No
    if len(vals) == 2 and {v.lower() for v in vals} == {"yes", "no"}:
        # keep canonical ids
        id_map = {"yes": "yes", "no": "no"}
        for o in (raw_opts or []):
            if isinstance(o, dict):
                v = str(o.get("value", "")).strip()
                lm = o.get("learn_more")
            else:
                v = str(o).strip()
                lm = None
            oid = id_map[v.lower()]
            out.append({"id": oid, "value": v, **({"learn_more": lm} if lm else {})})
        return out

    seen = {}
    for o in (raw_opts or []):
        if isinstance(o, dict):
            v = str(o.get("value", "")).strip()
            lm = o.get("learn_more")
        else:
            v = str(o).strip()
            lm = None
        oid = slugify(v)
        if oid in seen:
            seen[oid] += 1
            oid = f"{oid}_{seen[oid]}"
        else:
            seen[oid] = 1
        item = {"id": oid, "value": v}
        if lm:
            item["learn_more"] = lm
        out.append(item)
    return out


def map_answer_to_id(parent_q: Dict[str, Any], answer_token: str) -> str:
    """
    Convert v1 show_if answer token (value text) into v2 option id.
    """
    tok = str(answer_token).strip()
    for o in parent_q.get("options", []) or []:
        if str(o.get("value", "")).strip() == tok:
            return str(o.get("id"))
    # fallback case-insensitive
    for o in parent_q.get("options", []) or []:
        if str(o.get("value", "")).strip().lower() == tok.lower():
            return str(o.get("id"))
    # unknown -> slugify
    return slugify(tok)


def build_graph(v1: Dict[str, Any]) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """
    Build parent->children mapping from show_if and return normalized question dict.
    """
    norm: Dict[str, Dict[str, Any]] = {}
    children: Dict[str, List[str]] = {}

    for qid, meta in v1.items():
        q = {
            "id": str(qid),
            "question": str(meta.get("question", "")).strip(),
            "type": str(meta.get("type", "single")).lower(),
            "options": normalize_options(meta.get("options")),
            "show_if": meta.get("show_if"),
        }
        norm[str(qid)] = q

    for qid, q in norm.items():
        si = q.get("show_if")
        if not si:
            continue
        parent = str(si.get("id"))
        children.setdefault(parent, []).append(qid)

    return children, norm


def build_topic_tree(root_id: str, children_map: Dict[str, List[str]], norm: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Convert a root question into v2 hierarchical node:
    - node includes its children grouped by trigger (equals/contains)
    """
    root = norm[root_id]

    node = {
        "id": root["id"],
        "question": root["question"],
        "type": root["type"],
        "options": root["options"],
        "children": [],
    }

    # group children by the parent's show_if trigger on that child
    ch_ids = children_map.get(root_id, [])
    if not ch_ids:
        return node

    groups: Dict[str, Dict[str, Any]] = {}  # key -> {"trigger":..., "nodes":[...]}

    for cid in ch_ids:
        cq = norm[cid]
        si = cq.get("show_if") or {}
        ans = si.get("answer")

        parent_type = root["type"]
        if parent_type == "multiple":
            trig_type = "contains"
            opt_id = map_answer_to_id(root, ans)
            trig = {"type": trig_type, "option_id": opt_id, "option_value": str(ans)}
            gkey = f"contains:{opt_id}"
        else:
            trig_type = "equals"
            ans_id = map_answer_to_id(root, ans)
            trig = {"type": trig_type, "answer_id": ans_id, "answer_value": str(ans)}
            gkey = f"equals:{ans_id}"

        if gkey not in groups:
            groups[gkey] = {"trigger": trig, "nodes": []}

        # recurse: child can also be a parent
        child_node = {
            "id": cq["id"],
            "question": cq["question"],
            "type": cq["type"],
            "options": cq["options"],
        }
        # attach grandchildren if any
        if cid in children_map:
            child_node = build_topic_tree(cid, children_map, norm)
        groups[gkey]["nodes"].append(child_node)

    # If root is a yes/no gate, order groups so "yes" trigger is first
    # This is only cosmetic but helps readability and LLM focus.
    ordered_groups = list(groups.values())
    def group_rank(g):
        t = g.get("trigger", {})
        if t.get("type") == "equals" and (t.get("answer_id") == "yes" or str(t.get("answer_value", "")).lower() == "yes"):
            return 0
        return 1
    ordered_groups.sort(key=group_rank)

    node["children"] = ordered_groups
    return node


def migrate_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        v1 = json.load(fh)

    children_map, norm = build_graph(v1)

    # root candidates: those not shown by show_if
    roots = [qid for qid, q in norm.items() if not q.get("show_if")]
    # topic roots: those that have children
    topic_roots = [qid for qid in roots if qid in children_map]

    namespace = os.path.splitext(os.path.basename(path))[0]

    topics = []
    for rid in topic_roots:
        root = norm[rid]
        topics.append(
            {
                "id": rid,
                "title": root["question"][:140],
                "root": build_topic_tree(rid, children_map, norm),
                "inference": {
                    "child_positive_sets_gate": True,
                    "gate_yes_expands_children": True,
                },
            }
        )

    # always questions: roots that have no children
    always = []
    for rid in roots:
        if rid in topic_roots:
            continue
        q = norm[rid]
        always.append(
            {
                "id": q["id"],
                "question": q["question"],
                "type": q["type"],
                "options": q["options"],
            }
        )

    out = {
        "schema_version": 2,
        "namespace": namespace,
        "topics": topics,
        "always": always,
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate questionnaire v1(show_if) JSON -> v2 hierarchical JSON")
    ap.add_argument("--input-dir", type=str, required=True, help="Directory of v1 json files")
    ap.add_argument("--output-dir", type=str, required=True, help="Directory to write v2 json files")
    ap.add_argument("--indent", type=int, default=2)
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    for name in sorted(os.listdir(args.input_dir)):
        if not name.endswith(".json"):
            continue
        in_path = os.path.join(args.input_dir, name)
        out_path = os.path.join(args.output_dir, name)
        v2 = migrate_file(in_path)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(v2, fh, ensure_ascii=False, indent=args.indent)
        print(f"[OK] {name} -> v2")

    print("Done.")


if __name__ == "__main__":
    main()
