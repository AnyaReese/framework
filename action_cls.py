"""
action_cls.py

Action types and executor glue.

This file is kept intentionally small:
- ActionType enum (must align with gpt_cls.ActionType)
- Action executor that works with:
    - appium_android.AndroidAppiumClient
    - ui_cls.BaseUI vid_map nodes (id -> node dict with frames)
- Supports custom recovery actions:
    click / input / wait / back / restart / complete / none

We DO NOT touch utils.py or cnn_cls; this module is self-contained.

Important robustness notes:
- element_id must be from vid_map (post-processed UI), not resource-id.
- INPUT action:
    - taps target (if element_id provided)
    - then uses appium.type_text()
- WAIT action:
    - `text` may contain number seconds (string), otherwise default short wait.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from appium_android import AndroidAppiumClient
from ui_cls import BaseUI

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    CLICK = "click"
    INPUT = "input"
    WAIT = "wait"
    BACK = "back"
    RESTART = "restart"
    COMPLETE = "complete"
    NONE = "none"


@dataclass
class ActionStep:
    action: ActionType
    element_id: Optional[int] = None
    text: Optional[str] = None
    priority: int = 1
    reasoning: str = ""


class AndroidActionExecutor:
    """
    Execute ActionStep against a device.

    Note:
    - For safety, we don't implement destructive operations here.
    - Restart requires package+activity in config (optional).
    WHEN USED: optional adapter if you wire GPT actions directly through this executor (parallel to WorkflowRunner's inline executor).
    POSITION: sits between LLM ActionStep outputs and appium_android primitives when not using WorkflowRunner._execute_action.
    """

    def __init__(self, appium: AndroidAppiumClient, package: str = "", activity: Optional[str] = None):
        self.appium = appium
        self.package = package
        self.activity = activity

    def execute(self, step: ActionStep, vid_map: Dict[int, Dict[str, Any]]) -> bool:
        # WHEN: called per proposed ActionStep in custom flows or tests; expects vid_map from BaseUI.post_process_ui.
        # IMPORTANCE: enforces basic safety (checks element_id presence, restart package) and centralizes tap/input/back semantics.
        try:
            at = step.action
            if isinstance(at, str):
                at = ActionType(at)

            if at == ActionType.CLICK:
                return self._click(step.element_id, vid_map)

            if at == ActionType.INPUT:
                return self._input(step.element_id, step.text, vid_map)

            if at == ActionType.WAIT:
                sec = self._parse_wait(step.text)
                time.sleep(sec)
                return True

            if at == ActionType.BACK:
                self.appium.back()
                return True

            if at == ActionType.RESTART:
                if not self.package:
                    logger.warning("RESTART requested but package not configured")
                    return False
                try:
                    self.appium.force_stop(self.package)
                    time.sleep(0.5)
                    self.appium.ensure_foreground(self.package, self.activity)
                    return True
                except Exception:
                    logger.debug("restart failed", exc_info=True)
                    return False

            if at in (ActionType.COMPLETE, ActionType.NONE):
                return True

            logger.warning("Unknown action type: %s", step.action)
            return False
        except Exception:
            logger.debug("Action execution failed", exc_info=True)
            return False

    # -----------------------
    # Action primitives
    # -----------------------

    def _click(self, element_id: Optional[int], vid_map: Dict[int, Dict[str, Any]]) -> bool:
        if not element_id:
            return False
        node = vid_map.get(int(element_id))
        if not node:
            logger.warning("click: element_id=%s not found in vid_map", element_id)
            return False
        if not node.get("enabled", True):
            logger.warning("click: element_id=%s disabled", element_id)
            return False
        center = BaseUI.get_center(node)
        self.appium.tap(center["x"], center["y"])
        return True

    def _input(self, element_id: Optional[int], text: Optional[str], vid_map: Dict[int, Dict[str, Any]]) -> bool:
        # Tap the element if provided (help focus correct field)
        if element_id:
            ok = self._click(element_id, vid_map)
            if not ok:
                # Still try typing, sometimes focus is already correct
                logger.debug("input: click focus failed; trying type anyway")

        # Type text using robust method in appium_android
        self.appium.type_text("" if text is None else str(text))
        return True

    @staticmethod
    def _parse_wait(text: Optional[str]) -> float:
        if not text:
            return 0.7
        try:
            return float(text)
        except Exception:
            return 0.7


__all__ = ["ActionType", "ActionStep", "AndroidActionExecutor"]
