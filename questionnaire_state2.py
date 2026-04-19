"""
questionnaire_state2.py

Minimal experimental state layer for the new router/block workflow.

Current scope:
1. Read generated `questionnaire_routers.json`.
2. Read generated `questionnaire_blocks.json`.
3. Keep only three runtime structures:
   - `routers`: all router questions
   - `blocks`: all blocks, preserving each block's internal local structure
   - `block_status`: runtime state for each block

Design note:
- The "topic" here means a short human-readable summary for a block.
- It is NOT the old tree topic id.
- For now `topic` is initialized to "" and will be filled later by another step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class QuestionnaireState:
    """
    Minimal execution-view state.

    Public structures after loading:
    - self.routers: List[dict]
    - self.blocks: List[dict]
    - self.block_status: Dict[block_id, dict]
    """

    def __init__(self) -> None:
        self.routers: List[Dict[str, Any]] = []
        self.blocks: List[Dict[str, Any]] = []
        self.block_status: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def load_from_questionnaire_dir(questionnaire_dir: str) -> "QuestionnaireState":
        """
        Load router/block data by passing the original questionnaire directory,
        for example:
        - questionnaire-v2/games
        - questionnaire-v2/social_apps
        - questionnaire-v2/others

        The matching generated split directory is inferred automatically.
        """
        qdir = Path(questionnaire_dir).resolve()
        collection_name = qdir.name
        project_root = Path(__file__).resolve().parent
        split_dir = project_root / "mytest2" / "questionnaire_handler" / "chain_debug" / f"{collection_name}_split"

        inst = QuestionnaireState()
        inst.load_routers_and_blocks(str(split_dir))
        return inst

    def load_routers_and_blocks(self, split_dir: str) -> None:
        """
        Read and normalize:
        - questionnaire_routers.json
        - questionnaire_blocks.json

        Output is stored into:
        - self.routers
        - self.blocks
        - self.block_status
        """
        root = Path(split_dir).resolve()
        routers_path = root / "questionnaire_routers.json"
        blocks_path = root / "questionnaire_blocks.json"

        self.routers = []
        self.blocks = []
        self.block_status = {}

        if routers_path.exists():
            self.routers = self._read_routers_json(routers_path)

        if blocks_path.exists():
            self.blocks = self._read_blocks_json(blocks_path)

        self._init_block_status()

    def _read_routers_json(self, path: Path) -> List[Dict[str, Any]]:
        """
        Normalize all router questions into one flat list.

        Each router item keeps only the fields we currently care about.
        """
        data = self._read_json(path)
        routers: List[Dict[str, Any]] = []

        for namespace, payload in (data or {}).items():
            for raw in payload.get("router_questions", []) or []:
                question_id = str(raw.get("question_id") or "")
                routers.append(
                    {
                        "namespace": namespace,
                        "question_id": question_id,
                        "question_full_id": f"{namespace}.{question_id}",
                        "router_name": str(raw.get("router_name") or f"{namespace}.{question_id}"),
                        "question": str(raw.get("question") or ""),
                        "type": str(raw.get("type") or "single").lower(),
                        "options": list(raw.get("options") or []),
                    }
                )

        return routers

    def _read_blocks_json(self, path: Path) -> List[Dict[str, Any]]:
        """
        Normalize all blocks into one flat list.

        Important:
        - The OUTER container is a list for easy traversal/debugging.
        - Each block itself still preserves its LOCAL tree structure via:
          - question_ids / question_full_ids
          - questions
          - edges
          - parent_split_node
          - split_trigger
          - router_path
        """
        data = self._read_json(path)
        blocks: List[Dict[str, Any]] = []

        for namespace, payload in (data or {}).items():
            for raw in payload.get("blocks", []) or []:
                question_ids = [str(x) for x in (raw.get("question_ids") or [])]
                question_full_ids = [f"{namespace}.{qid}" for qid in question_ids]

                blocks.append(
                    {
                        "namespace": namespace,
                        "block_id": str(raw.get("block_id") or ""),
                        "block_name": str(raw.get("block_name") or raw.get("block_id") or ""),
                        "topic": "",
                        "entry_kind": str(raw.get("entry_kind") or ""),
                        "entry_label": str(raw.get("entry_label") or ""),
                        "parent_split_node": raw.get("parent_split_node"),
                        "split_trigger": raw.get("split_trigger"),
                        "router_path": list(raw.get("router_path") or []),
                        "question_ids": question_ids,
                        "question_full_ids": question_full_ids,
                        "questions": list(raw.get("questions") or []),
                        "edges": list(raw.get("edges") or []),
                    }
                )

        return blocks

    def _init_block_status(self) -> None:
        """
        Initialize runtime status for every block right after loading.
        """
        self.block_status = {}

        for block in self.blocks:
            block_id = str(block.get("block_id") or "")
            self.block_status[block_id] = {
                "block_id": block_id,
                "block_name": str(block.get("block_name") or ""),
                "topic": str(block.get("topic") or ""),
                "visit_count": 0,
                "hit_count": 0,
            }

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)


__all__ = ["QuestionnaireState"]
