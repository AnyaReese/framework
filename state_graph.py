# state_graph.py (patched; compatible with your "updated" version)

"""
state_graph.py

ROLE:
- Store navigation states (state signatures) and transitions (edges) with replayable action payloads.
- Support best-effort replay after restart via shortest_action_path(src, dst) -> (state_path, action_path).

CRITICAL SEMANTICS (workflow depends on these; do NOT re-implement elsewhere):
1) add_edge() touches src/dst (visit_count++).
2) Therefore, "dst was new at discovery" MUST be computed BEFORE add_edge().
   => record_transition() returns dst_was_new_at_discovery (bool).
3) We must sometimes attach overlay/meta WITHOUT incrementing visit_count.
   => annotate() updates node metadata/flags without counting a "visit".

WHEN USED IN WORKFLOW:
- record_observation(sig): called when we are at a state without a clean predecessor edge
  (entry, post-restart landing, post-recovery reconciliation).
- record_transition(src,dst,action): called for EVERY executed UI action that yields a new snapshot.
  Action payloads are stored as {"actions": [ ... ]} (single-step is length-1 list).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class Edge:
    # Directed transition labeled with the executed action(s); count tracks stability for replay.
    # action must be a dict with "actions": [ ... ] (single-step stored as length-1 list).
    src: str
    dst: str
    action: Dict[str, Any] = field(default_factory=dict)
    count: int = 0
    last_ts: float = 0.0
    no_effect: bool = False
    # Replay verification stats (separate from discovery count).
    verified_ok: int = 0
    verified_fail: int = 0
    last_verified_ts: float = 0.0


@dataclass
class Node:
    # State signature node with visit metadata; overlay_kind used as lightweight filter.
    sig: str
    visit_count: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    overlay_kind: str = "none"
    meta: Dict[str, Any] = field(default_factory=dict)

    outgoing: Set[str] = field(default_factory=set)
    incoming: Set[str] = field(default_factory=set)


class StateGraph:
    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}
        # key: (src, dst, action_key)
        self.edges: Dict[Tuple[str, str, str], Edge] = {}
        # adjacency for path search
        self._adj: Dict[str, List[Edge]] = defaultdict(list)

    # -------------------------
    # Introspection helpers
    # -------------------------

    @staticmethod
    def _norm_overlay_kind(overlay_kind: Optional[str]) -> str:
        if not overlay_kind:
            return "none"
        if hasattr(overlay_kind, "value"):
            return str(getattr(overlay_kind, "value"))
        return str(overlay_kind)

    def has_state(self, sig: str) -> bool:
        """
        IPO:
          in : sig
          out: True if node exists
        WHEN called (workflow):
          - before record_transition() to reason about novelty at discovery time
        """
        return sig in self.nodes

    def get_node(self, sig: str) -> Optional[Node]:
        return self.nodes.get(sig)

    # -------------------------
    # Node touch / annotate
    # -------------------------

    def touch(self, sig: str, overlay_kind: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> Node:
        """
        IPO:
          in : sig; optional overlay/meta
          out: Node (created if needed) with visit_count incremented
        WHEN called:
          - record_observation()
          - add_edge() (for src and dst)
        """
        now = time.time()
        n = self.nodes.get(sig)
        if n is None:
            n = Node(sig=sig, visit_count=0, first_ts=now, last_ts=now, overlay_kind=self._norm_overlay_kind(overlay_kind), meta=meta or {})
            self.nodes[sig] = n
        n.visit_count += 1
        n.last_ts = now
        if overlay_kind is not None:
            n.overlay_kind = self._norm_overlay_kind(overlay_kind)
        if meta:
            n.meta.update(meta)
        return n

    def annotate(self, sig: str, overlay_kind: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> None:
        """
        IPO:
          in : sig; optional overlay/meta
          out: updates node flags/meta WITHOUT incrementing visit_count
        WHEN called (workflow):
          - after receiving NAV results (LLM1) to mark overlay status
          - whenever we want to attach extra metadata to existing nodes
        WHY:
          - add_edge/touch already counts visits; we must not inflate visit_count just to store flags.
        """
        now = time.time()
        n = self.nodes.get(sig)
        if n is None:
            # If node doesn't exist, we DO want to create it, but still do NOT count as a visit.
            n = Node(sig=sig, visit_count=0, first_ts=now, last_ts=now, overlay_kind=self._norm_overlay_kind(overlay_kind), meta=meta or {})
            self.nodes[sig] = n
            return
        n.last_ts = now
        if overlay_kind is not None:
            n.overlay_kind = self._norm_overlay_kind(overlay_kind)
        if meta:
            n.meta.update(meta)

    # -------------------------
    # Edges / transitions
    # -------------------------

    def _action_key(self, action: Dict[str, Any]) -> str:
        """
        Build a stable-ish key for an action payload.
        Expects action dicts with an `actions` list (single-step => list of len 1).
        """
        if not action:
            return "None:None:"

        parts: List[str] = []
        for a in action.get("actions") or []:
            parts.append(f"{a.get('action')}:{a.get('element_id')}:{a.get('text') or ''}")
        return "||".join(parts) or "None:None:"

    def action_key(self, action: Dict[str, Any]) -> str:
        """
        Public wrapper for building an action payload key.
        """
        return self._action_key(action)

    def get_edge(self, src: str, dst: str, action: Dict[str, Any]) -> Optional[Edge]:
        """
        Look up the exact stored edge instance for (src,dst,action_key).
        Returns None if not found.
        """
        try:
            akey = self._action_key(action)
            return self.edges.get((src, dst, akey))
        except Exception:
            return None

    def record_edge_verification(self, src: str, dst: str, action: Dict[str, Any], *, ok: bool) -> None:
        """
        Update replay verification stats for a specific stored edge.
        Intended to be called by workflow replay/navigation when an edge is used as part of a plan.
        """
        e = self.get_edge(src, dst, action)
        if e is None:
            return
        now = time.time()
        if ok:
            e.verified_ok += 1
            e.last_verified_ts = now
        else:
            e.verified_fail += 1

    def add_edge(self, src: str, dst: str, action: Dict[str, Any]) -> None:
        """
        IPO:
          in : src,dst,action_dict
          out: edge recorded; edge.count++ ; src/dst nodes touched (visit_count++)
        WHEN called:
          - record_transition() (workflow should call record_transition, not add_edge directly)
        """
        akey = self._action_key(action)
        k = (src, dst, akey)
        e = self.edges.get(k)
        now = time.time()
        if e is None:
            e = Edge(src=src, dst=dst, action=dict(action), count=0, last_ts=now, no_effect=(src == dst))
            self.edges[k] = e
            self._adj[src].append(e)
        e.count += 1
        e.last_ts = now
        e.no_effect = (src == dst)

        self.touch(src)
        self.touch(dst)
        self.nodes[src].outgoing.add(dst)
        self.nodes[dst].incoming.add(src)

    def add_edge_no_touch(self, src: str, dst: str, action: Dict[str, Any]) -> None:
        """
        Record an edge WITHOUT incrementing src/dst visit_count.

        WHY:
          - Probe/return edges can be very frequent and will otherwise drown visit_count-based
            frontier heuristics, while still being useful for replay graph connectivity.
        """
        akey = self._action_key(action)
        k = (src, dst, akey)
        e = self.edges.get(k)
        now = time.time()
        if e is None:
            e = Edge(src=src, dst=dst, action=dict(action), count=0, last_ts=now, no_effect=(src == dst))
            self.edges[k] = e
            self._adj[src].append(e)
        e.count += 1
        e.last_ts = now
        e.no_effect = (src == dst)

        # Ensure nodes exist but do not count as "visits".
        if src not in self.nodes:
            self.nodes[src] = Node(sig=src, visit_count=0, first_ts=now, last_ts=now)
        else:
            self.nodes[src].last_ts = now
        if dst not in self.nodes:
            self.nodes[dst] = Node(sig=dst, visit_count=0, first_ts=now, last_ts=now)
        else:
            self.nodes[dst].last_ts = now

        self.nodes[src].outgoing.add(dst)
        self.nodes[dst].incoming.add(src)

    def record_observation(self, sig: str, overlay_kind: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> Node:
        """
        IPO:
          in : sig observed without a clean predecessor action edge
          out: touch(sig) -> increments visit_count exactly once
        WHEN called (workflow):
          - entry state (no predecessor)
          - post-restart landing state
          - post-recovery reconciliation when we can't safely label the move as an action edge
        """
        return self.touch(sig, overlay_kind=overlay_kind, meta=meta)

    def record_transition(
        self,
        src: str,
        dst: str,
        action: Dict[str, Any],
        *,
        touch: bool = True,
        dst_overlay_kind: Optional[str] = None,
        dst_meta: Optional[Dict[str, Any]] = None,
        src_overlay_kind: Optional[str] = None,
        src_meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        IPO:
          in : (src, dst, action_dict) and optional overlay/meta annotations
               action_dict may represent a single step or include an `actions` list for multi-step probes.
          out: dst_was_new_at_discovery (bool)

        WHEN called (workflow):
          - EVERY time we execute something and then capture a new authoritative snapshot
            (probe, forward, overlay dismissal, recovery step, replay step, drift).

        CRITICAL:
          - dst_was_new_at_discovery is computed BEFORE add_edge(), because add_edge touches nodes.
        """
        dst_was_new = (dst not in self.nodes)
        if touch:
            self.add_edge(src, dst, action)
        else:
            self.add_edge_no_touch(src, dst, action)

        # annotate WITHOUT incrementing (avoid double-touch inflation)
        if src_overlay_kind is not None or src_meta:
            self.annotate(src, overlay_kind=src_overlay_kind, meta=src_meta)
        if dst_overlay_kind is not None or dst_meta:
            self.annotate(dst, overlay_kind=dst_overlay_kind, meta=dst_meta)

        return dst_was_new

    # -------------------------
    # Frontier / replay
    # -------------------------

    def is_new_state(self, sig: str) -> bool:
        # Kept for compatibility, but note:
        # - With add_edge touching nodes, this reflects "currently rare", not "novel at discovery time".
        return sig not in self.nodes or self.nodes[sig].visit_count <= 1

    def frontier_states(self, max_items: int = 10) -> List[str]:
        # Heuristic frontier: prefer rarely visited, low-degree, recently seen non-overlay states.
        scored: List[Tuple[float, str]] = []
        now = time.time()
        for sig, n in self.nodes.items():
            if n.overlay_kind in ("dismiss", "loading"):
                continue
            out_deg = len(n.outgoing)
            age = now - n.last_ts
            score = 0.0
            score += 6.0 / max(1.0, float(n.visit_count))
            score += 5.0 / max(1.0, float(out_deg + 1))
            score += 1.5 / max(1.0, float(age / 60.0 + 1.0))
            scored.append((score, sig))
        scored.sort(reverse=True)
        return [s for _, s in scored[:max_items]]

    def frontier_hint(self) -> str:
        front = self.frontier_states(8)
        if not front:
            return "No frontier; likely stuck or saturated."
        return "Frontier: " + ", ".join([f[:8] for f in front])

    def shortest_action_path(self, src: str, dst: str, max_depth: int = 25) -> Tuple[List[str], List[Dict[str, Any]]]:
        """
        BFS over states, storing predecessor + edge action.

        Returns:
          (state_path, action_path)
        action_path length = len(state_path)-1

        Used for best-effort replay after app restart; edges with higher counts are preferred first.
        """
        if src == dst:
            return ([src], [])

        if src not in self.nodes or dst not in self.nodes:
            return ([], [])

        q = deque([src])
        prev: Dict[str, Optional[str]] = {src: None}
        prev_action: Dict[str, Optional[Dict[str, Any]]] = {src: None}
        depth: Dict[str, int] = {src: 0}

        while q:
            cur = q.popleft()
            if cur == dst:
                break
            if depth[cur] >= max_depth:
                continue

            edges = sorted(self._adj.get(cur, []), key=lambda e: e.count, reverse=True)
            for e in edges:
                nxt = e.dst
                if nxt not in prev:
                    prev[nxt] = cur
                    prev_action[nxt] = dict(e.action)
                    depth[nxt] = depth[cur] + 1
                    q.append(nxt)

        if dst not in prev:
            return ([], [])

        states: List[str] = []
        actions: List[Dict[str, Any]] = []
        cur = dst
        while cur is not None:
            states.append(cur)
            act = prev_action.get(cur)
            if act is not None:
                actions.append(act)
            cur = prev.get(cur)
        states.reverse()
        actions.reverse()
        return (states, actions)
