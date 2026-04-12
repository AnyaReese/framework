"""
questionnaire_state.py

Questionnaire state manager with:
- Support for BOTH:
    v1 flat schema (your existing files with `show_if`)
    v2 hierarchical schema (recommended; supports topic/gate structure explicitly)
- Open gaps detection
- Build question packs (focused subset) for LLM updates
- Sticky gate logic to avoid flip-flops when LLM lacks prior page context:
    - Once a gate is confidently "Yes", never downgrade to "No" unless explicit contradiction with very high confidence.
    - Multi-select answers are monotonic union by default (no removal unless explicit contradiction).
- Inference:
    - If a child question is positively answered, its parent gate is implied (child → parent).
    - If a parent gate is "Yes", children are prioritized in question packs (parent → children).

Why this matters:
- LLM only sees current screen; without memory it may revert answers.
- We store evidence summaries & confidence and include memory + current answers in LLM prompts.

Public API expected by workflow.py:
- QuestionnaireState.load_from_dir(dir)
- open_gaps()
- hierarchy_repr()            -> compact JSON-ish schema for LLM
- memory_summary(max_items)
- build_question_pack(limit)
- answers_for_ids(ids)
- apply_updates(QuestionnaireUpdate, context={...})
- progress_score()            -> coarse progress scalar for forward selection

Note:
- This file does not depend on utils.py; it is self-contained.

WHEN USED IN WORKFLOW:
- Instantiated once in main.py, then passed into WorkflowRunner.
- WorkflowRunner._schedule_state calls open_gaps/hierarchy_repr/build_question_pack/memory_summary/answers_for_ids each time a new state is scheduled for LLM2.
- WorkflowRunner._drain_futures calls apply_updates immediately when LLM2 completes, ensuring sticky progress before any recovery/restart.
IMPORTANCE: central source of truth for questionnaire progress and anti flip-flop memory across the entire run.
"""

from __future__ import annotations

import json
import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------
# Data models
# ---------------------------

@dataclass(frozen=True)
class Option:
    id: str
    value: str
    learn_more: Optional[str] = None


@dataclass
class Question:
    full_id: str
    namespace: str
    local_id: str
    qtype: str  # "single" | "multiple"
    question: str
    options: List[Option] = field(default_factory=list)
    is_gate: bool = False  # "Yes/No" single-select


@dataclass(frozen=True)
class Trigger:
    type: str  # "equals" | "contains"
    parent_full_id: str
    answer_id: Optional[str] = None     # equals
    option_id: Optional[str] = None     # contains
    answer_value: Optional[str] = None  # fallback
    option_value: Optional[str] = None  # fallback


@dataclass
class AnswerState:
    answer: Any = None
    confidence: float = 0.0
    evidence_refs: List[Any] = field(default_factory=list)
    evidence_summary: str = ""
    updated_at: float = 0.0
    visible: bool = True


# ---------------------------
# Helpers
# ---------------------------

def _ns(namespace: str, local_id: str) -> str:
    return f"{namespace}.{local_id}"


def _slugify(s: str, max_len: int = 40) -> str:
    import re
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return (s or "opt")[:max_len]


def _normalize_v1_options(raw_opts: Any) -> List[Option]:
    out: List[Option] = []
    seen: Dict[str, int] = {}
    for o in (raw_opts or []):
        if isinstance(o, dict):
            val = str(o.get("value", "")).strip()
            lm = o.get("learn_more")
        else:
            val = str(o).strip()
            lm = None
        oid = _slugify(val) if val else "opt"
        if oid in seen:
            seen[oid] += 1
            oid = f"{oid}_{seen[oid]}"
        else:
            seen[oid] = 1
        out.append(Option(id=oid, value=val, learn_more=lm))
    return out


def _is_yes_no(opts: List[Option]) -> bool:
    if len(opts) != 2:
        return False
    vals = {o.value.strip().lower() for o in opts}
    return vals == {"yes", "no"}


def _map_to_option_id(opts: List[Option], token: Any) -> Optional[str]:
    if token is None:
        return None
    s = str(token).strip()
    # accept ids
    for o in opts:
        if o.id == s:
            return o.id
    # accept exact value
    for o in opts:
        if o.value.strip() == s:
            return o.id
    # accept case-insensitive for yes/no
    for o in opts:
        if o.value.strip().lower() == s.lower():
            return o.id
    return None


def _option_value_lower(opts: List[Option], opt_id: str) -> str:
    for o in opts:
        if o.id == opt_id:
            return o.value.strip().lower()
    return str(opt_id).strip().lower()


# ---------------------------
# QuestionnaireState
# ---------------------------

class QuestionnaireState:
    def __init__(self) -> None:
        self.questions: Dict[str, Question] = {}
        self.state: Dict[str, AnswerState] = {}

        # visibility triggers: child -> list[Trigger] (AND)
        self.triggers: Dict[str, List[Trigger]] = {}

        # links
        self.children: Dict[str, List[str]] = {}
        self.parents: Dict[str, List[str]] = {}

        # topic membership (v2)
        self.topic_members: Dict[str, List[str]] = {}
        self.topics_meta: Dict[str, Dict[str, Any]] = {}

        # namespace grouping
        self.namespaces: Dict[str, List[str]] = {}

    # ----------- load -----------
    @staticmethod
    def load_from_dir(root_dir: str) -> "QuestionnaireState":
        # WHEN: process startup (main.py) to materialize questionnaire schemas into runtime state.
        inst = QuestionnaireState()
        for name in sorted(os.listdir(root_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(root_dir, name)
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            if isinstance(data, dict) and data.get("schema_version") == 2:
                inst._load_v2(data, fallback_namespace=os.path.splitext(name)[0])
            else:
                inst._load_v1(data, namespace=os.path.splitext(name)[0])

        inst._recompute_visibility()
        return inst

    def _register(self, q: Question) -> None:
        self.questions[q.full_id] = q
        self.state.setdefault(q.full_id, AnswerState())
        self.namespaces.setdefault(q.namespace, []).append(q.full_id)

    def _add_trigger(self, child: str, trig: Trigger) -> None:
        self.triggers.setdefault(child, []).append(trig)
        self.children.setdefault(trig.parent_full_id, []).append(child)
        self.parents.setdefault(child, []).append(trig.parent_full_id)

    # ---- v1 loader ----
    def _load_v1(self, v1: Dict[str, Any], namespace: str) -> None:
        # questions
        for local_id, meta in (v1 or {}).items():
            opts = _normalize_v1_options(meta.get("options"))
            full_id = _ns(namespace, str(local_id))
            qtype = str(meta.get("type", "single")).lower()
            q = Question(
                full_id=full_id,
                namespace=namespace,
                local_id=str(local_id),
                qtype=qtype,
                question=str(meta.get("question", "")).strip(),
                options=opts,
                is_gate=(qtype == "single" and _is_yes_no(opts)),
            )
            self._register(q)

        # triggers from show_if
        for local_id, meta in (v1 or {}).items():
            si = meta.get("show_if")
            if not si:
                continue
            child = _ns(namespace, str(local_id))
            parent = _ns(namespace, str(si.get("id")))
            dep = si.get("answer")

            parent_q = self.questions.get(parent)
            if not parent_q:
                continue

            if parent_q.qtype == "multiple":
                opt_id = _map_to_option_id(parent_q.options, dep)
                trig = Trigger(type="contains", parent_full_id=parent, option_id=opt_id, option_value=None if opt_id else str(dep))
            else:
                ans_id = _map_to_option_id(parent_q.options, dep)
                trig = Trigger(type="equals", parent_full_id=parent, answer_id=ans_id, answer_value=None if ans_id else str(dep))
            self._add_trigger(child, trig)

        # infer topics best-effort (root parents with children)
        self._infer_topics(namespace)

    # ---- v2 loader ----
    def _load_v2(self, v2: Dict[str, Any], fallback_namespace: str) -> None:
        namespace = str(v2.get("namespace") or fallback_namespace)

        def read_opts(raw) -> List[Option]:
            if not raw:
                return []
            out: List[Option] = []
            for o in raw:
                if isinstance(o, dict):
                    out.append(Option(id=str(o.get("id")), value=str(o.get("value", "")), learn_more=o.get("learn_more")))
                else:
                    val = str(o)
                    out.append(Option(id=_slugify(val), value=val))
            return out

        def walk_node(node: Dict[str, Any], topic_id: str, parent: Optional[Tuple[str, Dict[str, Any]]] = None):
            local_id = str(node["id"])
            full_id = _ns(namespace, local_id)
            qtype = str(node.get("type", "single")).lower()
            opts = read_opts(node.get("options"))

            if full_id not in self.questions:
                self._register(
                    Question(
                        full_id=full_id,
                        namespace=namespace,
                        local_id=local_id,
                        qtype=qtype,
                        question=str(node.get("question", "")).strip(),
                        options=opts,
                        is_gate=(qtype == "single" and _is_yes_no(opts)),
                    )
                )

            self.topic_members.setdefault(topic_id, []).append(full_id)

            if parent is not None:
                parent_full, trig = parent
                ttype = trig.get("type")
                if ttype == "equals":
                    self._add_trigger(
                        full_id,
                        Trigger(
                            type="equals",
                            parent_full_id=parent_full,
                            answer_id=trig.get("answer_id"),
                            answer_value=trig.get("answer_value"),
                        ),
                    )
                elif ttype == "contains":
                    self._add_trigger(
                        full_id,
                        Trigger(
                            type="contains",
                            parent_full_id=parent_full,
                            option_id=trig.get("option_id"),
                            option_value=trig.get("option_value"),
                        ),
                    )

            for grp in node.get("children", []) or []:
                trig = grp.get("trigger") or {}
                for ch in grp.get("nodes", []) or []:
                    walk_node(ch, topic_id, parent=(full_id, trig))

        # topics
        for t in v2.get("topics", []) or []:
            tid = f"{namespace}::{t.get('id')}"
            self.topics_meta[tid] = {
                "id": tid,
                "title": t.get("title", ""),
                "namespace": namespace,
                "inference": t.get("inference", {}),
                "hints": t.get("hints", {}),
            }
            root = t.get("root")
            if root:
                walk_node(root, tid)

        # always
        for n in v2.get("always", []) or []:
            local_id = str(n["id"])
            full_id = _ns(namespace, local_id)
            qtype = str(n.get("type", "single")).lower()
            opts = read_opts(n.get("options"))
            if full_id not in self.questions:
                self._register(
                    Question(
                        full_id=full_id,
                        namespace=namespace,
                        local_id=local_id,
                        qtype=qtype,
                        question=str(n.get("question", "")).strip(),
                        options=opts,
                        is_gate=(qtype == "single" and _is_yes_no(opts)),
                    )
                )

        if not self.topics_meta:
            self._infer_topics(namespace)

    def _infer_topics(self, namespace: str) -> None:
        # roots are those without triggers
        roots = [qid for qid in self.namespaces.get(namespace, []) if qid not in self.triggers]
        topic_roots = [qid for qid in roots if qid in self.children]
        for r in topic_roots:
            tid = f"{namespace}::{self.questions[r].local_id}"
            self.topics_meta[tid] = {
                "id": tid,
                "title": self.questions[r].question[:120],
                "namespace": namespace,
                "inference": {"child_positive_sets_gate": True, "gate_yes_expands_children": True},
                "hints": {},
            }
            # subtree members
            members: List[str] = []
            stack = [r]
            seen = set()
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                members.append(cur)
                for ch in self.children.get(cur, []):
                    stack.append(ch)
            self.topic_members[tid] = members

    # -------- visibility --------
    def _trigger_satisfied(self, trig: Trigger) -> bool:
        pq = self.questions.get(trig.parent_full_id)
        ps = self.state.get(trig.parent_full_id)
        if not pq or not ps:
            return False

        if pq.qtype == "single":
            ans = ps.answer
            want = trig.answer_id or _map_to_option_id(pq.options, trig.answer_value)
            return ans is not None and str(ans) == str(want)

        if pq.qtype == "multiple":
            ans = ps.answer or []
            if not isinstance(ans, list):
                ans = [ans]
            want = trig.option_id or _map_to_option_id(pq.options, trig.option_value)
            return want is not None and str(want) in {str(x) for x in ans}

        return False

    def _is_visible(self, qid: str) -> bool:
        rules = self.triggers.get(qid) or []
        for trig in rules:
            if not self._trigger_satisfied(trig):
                return False
        return True

    def _recompute_visibility(self) -> None:
        for qid in self.questions.keys():
            self.state[qid].visible = self._is_visible(qid)

    # -------- gaps / scoring --------
    def open_gaps(self) -> List[str]:
        # WHEN: per-state scheduling in WorkflowRunner to tell LLMs which questions remain.
        out: List[str] = []
        for qid, q in self.questions.items():
            st = self.state[qid]
            if not st.visible:
                continue
            if q.qtype == "single":
                if st.answer is None:
                    out.append(qid)
            else:
                if not st.answer:
                    out.append(qid)
        return out

    def progress_score(self) -> float:
        """
        Coarse metric: number of visible answered questions.
        """
        # WHEN: logged each loop to show progress and used implicitly in forward scoring heuristics.
        done = 0
        vis = 0
        for qid, q in self.questions.items():
            st = self.state[qid]
            if not st.visible:
                continue
            vis += 1
            if q.qtype == "single":
                if st.answer is not None:
                    done += 1
            else:
                if st.answer:
                    done += 1
        return float(done) / max(1.0, float(vis))

    # -------- hierarchy for LLM --------
    def hierarchy_repr(self, max_questions: int = 300) -> Dict[str, Any]:
        """
        Compact representation of schema for LLM:
        - topics (roots + children ids)
        - question meta (type/options)
        """
        # WHEN: supplied to LLM2 so it can reason about parent/child gates while staying token-bounded.
        qmeta: Dict[str, Any] = {}
        for i, (qid, q) in enumerate(self.questions.items()):
            if i >= max_questions:
                break
            qmeta[qid] = {
                "id": qid,
                "type": q.qtype,
                "question": q.question[:140],
                "options": [{"id": o.id, "value": o.value} for o in q.options],
                "is_gate": q.is_gate,
                "parents": self.parents.get(qid, []),
                "children": self.children.get(qid, []),
            }

        topics = []
        for tid, meta in self.topics_meta.items():
            topics.append(
                {
                    "id": tid,
                    "title": meta.get("title", ""),
                    "members": self.topic_members.get(tid, [])[:200],
                }
            )

        return {"topics": topics[:50], "questions": qmeta}

    # -------- topic helpers (LLM2 router/filler) --------
    def topic_tree_shallow(self, max_topics: int = 60, example_questions_per_topic: int = 2) -> List[Dict[str, Any]]:
        """
        Lightweight topic list for topic routing (LLM2-1).
        Each item is stable and small: {topic_id,title,keywords,example_questions}.
        """
        out: List[Dict[str, Any]] = []
        for tid, meta in self.topics_meta.items():
            title = str(meta.get("title") or "").strip()
            hints = meta.get("hints") or {}
            keywords = hints.get("keywords") if isinstance(hints, dict) else None
            if not isinstance(keywords, list):
                keywords = []

            examples: List[str] = []
            for qid in (self.topic_members.get(tid) or [])[:50]:
                q = self.questions.get(qid)
                if not q:
                    continue
                if q.question:
                    examples.append(q.question[:120])
                if len(examples) >= example_questions_per_topic:
                    break

            out.append(
                {
                    "topic_id": tid,
                    "title": title[:140],
                    "keywords": [str(k)[:40] for k in (keywords or [])][:10],
                    "example_questions": examples[:example_questions_per_topic],
                }
            )
            if len(out) >= max_topics:
                break

        return out

    def open_gaps_in_topic(self, topic_id: str) -> List[str]:
        members = set(self.topic_members.get(topic_id) or [])
        if not members:
            return []
        out: List[str] = []
        for qid in self.open_gaps():
            if qid in members:
                out.append(qid)
        return out

    def build_topic_question_pack(self, topic_id: str, limit: int = 18) -> List[Dict[str, Any]]:
        """
        Like build_question_pack(), but restricted to a single topic.
        """
        members = set(self.topic_members.get(topic_id) or [])
        if not members:
            return []

        gaps = [qid for qid in self.open_gaps() if qid in members]
        selected: List[str] = []
        seen = set()

        def add(qid: str):
            if qid in seen:
                return
            if qid not in self.questions:
                return
            if qid not in members:
                return
            if not self.state[qid].visible:
                return
            seen.add(qid)
            selected.append(qid)

        for qid in gaps:
            add(qid)
            # parent yes => include visible children inside same topic
            for parent in self.parents.get(qid, []):
                if parent not in members:
                    continue
                pq = self.questions.get(parent)
                ps = self.state.get(parent)
                if pq and ps and pq.is_gate and ps.answer is not None:
                    if _option_value_lower(pq.options, str(ps.answer)) == "yes":
                        for ch in self.children.get(parent, []):
                            if ch in members:
                                add(ch)

            if len(selected) >= limit:
                break

        pack: List[Dict[str, Any]] = []
        for qid in selected[:limit]:
            q = self.questions[qid]
            st = self.state[qid]
            pack.append(
                {
                    "id": qid,
                    "type": q.qtype,
                    "question": q.question,
                    "options": [{"id": o.id, "value": o.value, "learn_more": o.learn_more} for o in q.options],
                    "current_answer": st.answer,
                    "current_confidence": st.confidence,
                    "evidence_summary": st.evidence_summary,
                    "parents": self.parents.get(qid, []),
                    "children": self.children.get(qid, []),
                }
            )
        return pack

    def topic_memory_summary(self, topic_id: str, max_items: int = 18) -> List[Dict[str, Any]]:
        members = set(self.topic_members.get(topic_id) or [])
        if not members:
            return []
        items = self.memory_summary(max_items=max_items * 2)
        out = [it for it in items if it.get("id") in members]
        return out[:max_items]

    def answers_digest_for_topic(self, topic_id: str) -> str:
        """
        Compact digest of current answers within a topic.
        Used as a cache key component for topic fill (LLM2-2).
        """
        members = list(self.topic_members.get(topic_id) or [])
        payload: Dict[str, Any] = {}
        for qid in sorted(members):
            st = self.state.get(qid)
            if not st:
                continue
            payload[qid] = st.answer
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.md5(blob.encode("utf-8")).hexdigest()

    def active_topics_top(self, limit: int = 6) -> List[Dict[str, Any]]:
        """
        Minimal, low-volatility questionnaire context for LLM1 navigation:
        returns top topics with remaining open gaps.
        """
        gaps = set(self.open_gaps())
        scored: List[Tuple[int, str]] = []
        for tid, members in (self.topic_members or {}).items():
            if not members:
                continue
            cnt = sum(1 for qid in members if qid in gaps)
            if cnt > 0:
                scored.append((cnt, tid))
        scored.sort(reverse=True)

        out: List[Dict[str, Any]] = []
        for cnt, tid in scored[: max(0, int(limit))]:
            meta = self.topics_meta.get(tid) or {}
            title = str(meta.get("title") or "").strip()
            hints = meta.get("hints") or {}
            keywords = hints.get("keywords") if isinstance(hints, dict) else None
            if not isinstance(keywords, list):
                keywords = []
            out.append(
                {
                    "topic_id": tid,
                    "title": title[:140],
                    "open_gaps": int(cnt),
                    "keywords": [str(k)[:40] for k in (keywords or [])][:10],
                }
            )
        return out

    # -------- pack building --------
    def build_question_pack(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Build a prioritized pack:
        - start from open gaps
        - if a gap has a visible parent gate that's yes, pull siblings/children too
        """
        # WHEN: before every LLM2 call; keeps prompt focused on the most actionable gaps for current screen.
        gaps = self.open_gaps()
        selected: List[str] = []
        seen = set()

        def add(qid: str):
            if qid in seen:
                return
            if qid not in self.questions:
                return
            if not self.state[qid].visible:
                return
            seen.add(qid)
            selected.append(qid)

        for qid in gaps:
            add(qid)
            # parent yes => include children
            for parent in self.parents.get(qid, []):
                pq = self.questions.get(parent)
                ps = self.state.get(parent)
                if pq and ps and pq.is_gate and ps.answer is not None:
                    # if yes, include parent's other visible children
                    if _option_value_lower(pq.options, str(ps.answer)) == "yes":
                        for ch in self.children.get(parent, []):
                            add(ch)

            if len(selected) >= limit:
                break

        # construct pack entries
        pack: List[Dict[str, Any]] = []
        for qid in selected[:limit]:
            q = self.questions[qid]
            st = self.state[qid]
            pack.append(
                {
                    "id": qid,
                    "type": q.qtype,
                    "question": q.question,
                    "options": [{"id": o.id, "value": o.value, "learn_more": o.learn_more} for o in q.options],
                    "current_answer": st.answer,
                    "current_confidence": st.confidence,
                    "evidence_summary": st.evidence_summary,
                    "parents": self.parents.get(qid, []),
                    "children": self.children.get(qid, []),
                }
            )
        return pack

    def answers_for_ids(self, ids: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for qid in ids:
            if qid in self.state:
                out[qid] = self.state[qid].answer
        return out

    def memory_summary(self, max_items: int = 30) -> List[Dict[str, Any]]:
        """
        Provide stable memory to LLM:
        - focus on gate questions answered YES and any high-confidence answers
        """
        # WHEN: provided to LLM2 on every call so it understands prior yes decisions and avoids flip-flops.
        items: List[Tuple[float, str]] = []

        for qid, q in self.questions.items():
            st = self.state[qid]
            if not st.answer:
                continue
            if q.is_gate:
                if _option_value_lower(q.options, str(st.answer)) == "yes":
                    items.append((st.confidence, qid))
            else:
                if st.confidence >= 0.7:
                    items.append((st.confidence, qid))

        items.sort(reverse=True)
        out: List[Dict[str, Any]] = []
        for conf, qid in items[:max_items]:
            q = self.questions[qid]
            st = self.state[qid]
            out.append(
                {
                    "id": qid,
                    "question": q.question[:120],
                    "answer": st.answer,
                    "confidence": conf,
                    "evidence_summary": st.evidence_summary[:240],
                }
            )
        return out

    # -------- apply updates (sticky + inference) --------
    def apply_updates(self, q_update: Any, context: Optional[Dict[str, Any]] = None) -> None:
        """
        Apply QuestionnaireUpdate results.
        Accepts:
          - QuestionnaireUpdate (Pydantic)
          - dict with "proposed_updates"
        """
        # WHEN: invoked by WorkflowRunner._drain_futures as soon as LLM2 completes; persists answers + evidence.
        if q_update is None:
            return

        proposed = None
        if isinstance(q_update, dict):
            proposed = q_update.get("proposed_updates") or []
        else:
            proposed = getattr(q_update, "proposed_updates", None) or []

        state_sig = (context or {}).get("state_sig")
        page_summary = (context or {}).get("page_summary", "")

        for it in proposed:
            if isinstance(it, dict):
                qid = it.get("question_id") or it.get("id")
                new_answer = it.get("new_answer")
                conf = float(it.get("confidence", 0.0))
                evidence = it.get("evidence_refs") or []
            else:
                qid = getattr(it, "question_id", None) or getattr(it, "id", None)
                new_answer = getattr(it, "new_answer", None)
                conf = float(getattr(it, "confidence", 0.0))
                evidence = getattr(it, "evidence_refs", None) or []

            if not qid or qid not in self.questions:
                continue

            q = self.questions[qid]
            st = self.state[qid]
            if not st.visible:
                continue

            # Normalize single-select output shape (LLM may emit list even for single-select).
            if q.qtype == "single" and isinstance(new_answer, list):
                new_answer = new_answer[0] if new_answer else None

            # Normalize to option ids
            if q.qtype == "multiple":
                raw_list = new_answer if isinstance(new_answer, list) else ([] if new_answer is None else [new_answer])
                mapped: List[str] = []
                for tok in raw_list:
                    oid = _map_to_option_id(q.options, tok)
                    if oid:
                        mapped.append(oid)
                new_norm = sorted(set(mapped))
            else:
                new_norm = _map_to_option_id(q.options, new_answer)

            # Sticky gate: don't downgrade yes->no without explicit contradiction and very high confidence
            if q.is_gate and st.answer is not None and new_norm is not None:
                prev_val = _option_value_lower(q.options, str(st.answer))
                new_val = _option_value_lower(q.options, str(new_norm))
                if prev_val == "yes" and new_val == "no" and conf < 0.95:
                    continue

            # Apply answers
            if q.qtype == "single":
                if st.answer is None:
                    if new_norm is not None:
                        st.answer = new_norm
                        st.confidence = conf
                else:
                    # only override if stronger confidence
                    if new_norm is not None and conf >= max(0.55, st.confidence + 0.05):
                        st.answer = new_norm
                        st.confidence = conf

            else:  # multiple -> union by default
                old = st.answer or []
                if not isinstance(old, list):
                    old = [old]
                old_set = {str(x) for x in old}
                if not old_set:
                    if new_norm:
                        st.answer = new_norm
                        st.confidence = conf
                else:
                    # union only (monotonic) unless extremely high confidence (removal not supported by default)
                    if new_norm and conf >= max(0.35, st.confidence * 0.6):
                        st.answer = sorted(old_set | {str(x) for x in new_norm})
                        st.confidence = max(st.confidence, conf)

            if evidence:
                st.evidence_refs.extend(evidence)

            if conf > 0.5 and evidence:
                bits = []
                if page_summary:
                    bits.append(f"page:{page_summary[:80]}")
                if state_sig:
                    bits.append(f"state:{str(state_sig)[:10]}")
                bits.append(f"evidence:{str(evidence)[:160]}")
                add = " | ".join(bits)
                if st.evidence_summary:
                    st.evidence_summary = (st.evidence_summary + " || " + add)[:800]
                else:
                    st.evidence_summary = add[:800]

            st.updated_at = time.time()

            # Inference: child positive => parent implied
            self._infer_upwards(qid, conf, evidence)

        self._recompute_visibility()

    def _infer_upwards(self, qid: str, conf: float, evidence: List[Any]) -> None:
        """
        If a child is positive:
        - satisfy its triggers (set parent option if possible)
        - if parent is gate and unanswered, set it to Yes.
        """
        q = self.questions[qid]
        st = self.state[qid]

        positive = False
        if q.qtype == "single":
            if st.answer is None:
                positive = False
            elif q.is_gate:
                positive = (_option_value_lower(q.options, str(st.answer)) == "yes")
            else:
                positive = True
        else:
            positive = bool(st.answer)

        if not positive:
            return

        for trig in (self.triggers.get(qid) or []):
            parent = trig.parent_full_id
            pq = self.questions.get(parent)
            ps = self.state.get(parent)
            if not pq or not ps:
                continue

            # satisfy parent selection implied by trigger
            if pq.qtype == "single" and trig.type == "equals":
                want = trig.answer_id or _map_to_option_id(pq.options, trig.answer_value)
                if want and ps.answer is None:
                    ps.answer = want
                    ps.confidence = max(ps.confidence, min(conf, 0.75))
                    if evidence:
                        ps.evidence_refs.extend(evidence)

            if pq.qtype == "multiple" and trig.type == "contains":
                want = trig.option_id or _map_to_option_id(pq.options, trig.option_value)
                if want:
                    cur = ps.answer or []
                    if not isinstance(cur, list):
                        cur = [cur]
                    cur_set = {str(x) for x in cur}
                    if want not in cur_set:
                        ps.answer = sorted(cur_set | {want})
                        ps.confidence = max(ps.confidence, min(conf, 0.70))
                        if evidence:
                            ps.evidence_refs.extend(evidence)

            # gate inference
            if pq.is_gate and ps.answer is None:
                yes_id = None
                for o in pq.options:
                    if o.value.strip().lower() == "yes":
                        yes_id = o.id
                        break
                if yes_id:
                    ps.answer = yes_id
                    ps.confidence = max(ps.confidence, min(conf, 0.70))
                    if evidence:
                        ps.evidence_refs.extend(evidence)

    # -------- export helpers --------
    def export_answers_by_namespace(self) -> Dict[str, Dict[str, Any]]:
        """
        Export human-readable values grouped by namespace (file).
        """
        out: Dict[str, Dict[str, Any]] = {}
        for ns, qids in self.namespaces.items():
            sec: Dict[str, Any] = {}
            for qid in qids:
                q = self.questions[qid]
                st = self.state[qid]
                if not st.visible:
                    continue
                sec[q.local_id] = self._answer_value(q, st)
            out[ns] = sec
        return out

    def _answer_value(self, q: Question, st: AnswerState) -> Any:
        if q.qtype == "single":
            if st.answer is None:
                return None
            for o in q.options:
                if o.id == st.answer:
                    return o.value
            return st.answer
        # multiple
        if not st.answer:
            return []
        ans_list = st.answer if isinstance(st.answer, list) else [st.answer]
        vals = []
        for a in ans_list:
            v = None
            for o in q.options:
                if o.id == a:
                    v = o.value
                    break
            vals.append(v or a)
        return vals


__all__ = ["QuestionnaireState", "Question", "Option", "Trigger", "AnswerState"]
