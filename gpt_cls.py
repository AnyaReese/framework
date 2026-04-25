# gpt_cls.py (patched; key change: per-candidate return strategies)

"""
gpt_cls.py

KEY CHANGE (requested):
- Each candidate action now carries its own return_method / return_actions (ActionCandidate).
  Reason: different probes on the same page may require different unwind strategies (e.g.,
  tab click vs. close vs. back). The global return_method is now only a fallback.

WHAT WE ADD:
- ActionCandidate wrapper (step + per-candidate return_method/return_actions).
- NavigationProposal.candidate_actions is List[ActionCandidate].
- Global return_method/return_actions remain as defaults for legacy prompts.

WORKFLOW CONTRACT:
- If a candidate supplies return_actions, they run first after that probe.
- BACK is a last resort for tab-back/custom contexts; per-candidate return overrides global.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Type, TypeVar, Union

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
logging.getLogger("openai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)


from utils import time_consumed, token_record  # do NOT modify user's utils.py


R = TypeVar("R", bound=BaseModel)


class ActionType(str, Enum):
    CLICK = "click"
    INPUT = "input"
    WAIT = "wait"
    BACK = "back"
    RESTART = "restart"
    COMPLETE = "complete"
    NONE = "none"

class OverlayKind(str, Enum):
    NONE = "none"
    DISMISS = "dismiss"      # permission/rate/cookie/etc
    WORKFLOW = "workflow"    # login/search/filter/form/otp
    LOADING = "loading"      # spinner / transition


class UIElementType(str, Enum):
    BUTTON = "Button"
    TEXT_BUTTON = "TextButton"
    ICON_BUTTON = "IconButton"
    INPUT_TEXT = "InputText"
    TOGGLE = "Toggle"
    CHECKBOX = "Checkbox"
    RADIO = "Radio"
    TAB = "Tab"
    LIST_ITEM = "ListItem"
    TEXT_ONLY = "TextOnly"
    ICON_ONLY = "IconOnly"
    DIALOG = "Dialog"
    OTHER_INTERACTABLE = "OtherInteractable"
    UNKNOWN = "Unknown"


class UIElement(BaseModel):
    id: int = Field(..., description="Stable id matching UI tree after post-processing")
    ui_type: UIElementType = Field(..., description="Element category")
    description: str = Field("", description="Inferred purpose or label (<=15 words)")
    text: str = Field("", description="Visible text/value if any")
    clickability: float = Field(0.0, description="0-1 likelihood the element is actionable")
    location: str = Field("", description="Relative position (e.g., top-left, header, nav bar)")


class TagSignal(BaseModel):
    tag: str = Field(..., description="Short taxonomy tag (snake_case preferred)")
    weight: float = Field(0.0, ge=0.0, le=1.0, description="0-1 strength of association")


class UIView(BaseModel):
    """
    A concise, model-generated interpretation of the current screen.
    Keep it SMALL (the prompt enforces item caps).
    """
    description: str = Field("", description="One-paragraph summary of layout and purpose")
    feedback_message: str = Field("", description="Visible status/toast/error text if any")
    is_alert_topmost: bool = Field(False, description="True if a blocking dialog/overlay is on top")
    hint_elements: List[UIElement] = Field(default_factory=list, description="Informative, mostly non-interactive elements")
    action_elements: List[UIElement] = Field(default_factory=list, description="Interactive elements")
    match_rate: float = Field(1.0, description="0-1 match confidence between UI tree + screenshot")

    @property
    def elements(self) -> List[UIElement]:
        return (self.hint_elements or []) + (self.action_elements or [])


class ActionStep(BaseModel):
    action: ActionType = Field(..., description="Concrete action type")
    element_id: Optional[int] = Field(None, description="UI element id for click/input")
    text: Optional[str] = Field(None, description="Text to input when action==input")
    priority: int = Field(0, description="Higher executes earlier when same group")
    reasoning: str = Field("", description="Short rationale (<=2 sentences). No chain-of-thought.")


class ActionCandidate(BaseModel):
    """
    Wraps one probe/forward plan with its own return strategy.

    Rationale:
      - Some probes need multi-step execution (e.g., input text then tap Confirm).
      - Different probes on the same page may require different unwind methods
        (tab click vs. close vs. back). Global return_method/return_actions are only defaults.
    """

    actions: List[ActionStep] = Field(
        ...,
        description="Ordered steps to perform this candidate (e.g., input then click). Must not be empty.",
        min_items=1,
    )
    return_method: Optional[Literal["back", "close", "tab-back", "custom", "none"]] = Field(
        None,
        description=(
            "Preferred return method AFTER this candidate executes. "
            "If null, the workflow inherits NavigationProposal.return_method."
        ),
    )
    return_actions: List[ActionStep] = Field(
        default_factory=list,
        description=(
            "Custom steps to restore the source state after THIS candidate. "
            "If empty, workflow falls back to candidate.return_method, then NavigationProposal defaults. (max 4)"
        ),
    )
    score: float = Field(
        0.0,
        ge=-1.0,
        le=1.0,
        description=(
            "LLM priority score for this candidate in [-1, 1]. "
            "Negative to deprioritize (e.g., navigate back), positive to favor."
        ),
    )

    tags: List[TagSignal] = Field(
        default_factory=list,
        description="Optional candidate-level tags bridging navigation to questionnaire topics (max 6).",
    )


class NavigationProposal(BaseModel):
    """
    LLM1 output: UI analysis + overlay resolution + exploration candidates.
    """
    state_sig: str = Field(..., description="Echo input state_sig for staleness/debug")
    ui_view: UIView = Field(..., description="Structured UI analysis (must be present)")

    page_summary: str = Field(..., description="One-line summary of current page")
    ui_type: str = Field("unknown", description="Page type classification (stable, reusable)")
    page_tags: List[TagSignal] = Field(default_factory=list, description="Page-level semantic tags (max 10)")
    tag_evidence: str = Field("", description="Optional short evidence for tags/ui_type (<=1 sentence)")
    key_interactables: List[int] = Field(default_factory=list, description="IDs worth attention (max 12)")

    overlay_kind: OverlayKind = Field(OverlayKind.NONE, description="Overlay type classification")
    overlay_reason: str = Field("", description="Why overlay_kind was chosen (<=1 sentence)")
    overlay_dismiss_actions: List[ActionStep] = Field(
        default_factory=list,
        description="Actions to dismiss blocking overlays (max 5); only for overlay_kind=dismiss",
    )
    workflow_hints: List[str] = Field(
        default_factory=list,
        description="Optional hints when overlay_kind=workflow (e.g., requires input, safe default query)",
    )

    candidate_actions: List[ActionCandidate] = Field(
        default_factory=list,
        description=(
            "Probe/advance candidates (max 10). Each candidate supplies 1-3 ordered actions "
            "and may include its own return_method/return_actions; otherwise global defaults apply."
        ),
    )

    # IMPORTANT: global return policy fallback (used when a candidate omits its own return_method/return_actions)
    return_method: Literal["back", "close", "tab-back", "custom", "none"] = Field(
        "back",
        description="Default return method after probes when a candidate does not provide its own return_method.",
    )
    return_actions: List[ActionStep] = Field(
        default_factory=list,
        description=(
            "Default custom steps to return to the source state after probes when a candidate does not "
            "provide its own return_actions (max 4)."
        ),
    )

    why_these_actions: str = Field("", description="Brief rationale linking actions to questionnaire evidence goals (no hidden CoT)")


class RecoveryProposal(BaseModel):
    """
    LLM3 output: recover to stable exploration state with custom actions allowed.
    """
    state_sig: str = Field(..., description="Echo input state_sig for staleness/debug")
    ui_view: UIView = Field(..., description="UI interpretation for recovery context")

    page_summary: str = Field(..., description="What screen looks like and why stuck")
    target_hint: str = Field("", description="Frontier/goal hint")
    overlay_kind: OverlayKind = Field(OverlayKind.NONE, description="Overlay type classification")
    overlay_reason: str = Field("", description="Why overlay_kind was chosen")
    candidate_actions: List[ActionStep] = Field(default_factory=list, description="Up to 5 steps to escape")
    why: str = Field("", description="Short explanation of recovery strategy (no hidden CoT)")


class ProposedUpdate(BaseModel):
    question_id: str
    new_answer: Union[str, List[str], None] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    evidence_refs: List[str] = Field(default_factory=list)
    note: str = ""


class QuestionnaireUpdate(BaseModel):
    detected_signals: List[str] = Field(
        default_factory=list,
        description="Evidence strings observed on this screen that may inform answers (conservative, short).",
    )
    proposed_updates: List[ProposedUpdate] = Field(
        default_factory=list,
        description="Minimal set of questionnaire changes justified by CURRENT SCREEN evidence.",
    )
    open_gaps_recommended: List[str] = Field(
        default_factory=list,
        description="IDs of gaps that should be prioritized next based on hints from this screen.",
    )
    conflicts: List[str] = Field(
        default_factory=list,
        description="Detected inconsistencies between current screen evidence and existing answers (string IDs).",
    )


class BlockFillResult(BaseModel):
    block_id: str = Field(..., description="Block id from questionnaire_blocks.json")
    detected_signals: List[str] = Field(default_factory=list, description="Short evidence strings observed for this block")
    proposed_updates: List[ProposedUpdate] = Field(default_factory=list, description="Question updates for this block only")
    conflicts: List[str] = Field(default_factory=list, description="Optional conflict notes for this block")


class BlocksFillResult(BaseModel):
    state_sig: str = Field("", description="Echoed UI state signature")
    block_results: List[BlockFillResult] = Field(default_factory=list, description="One result per answered block")


class RelevantTopic(BaseModel):
    topic_id: str = Field(..., description="Stable topic_id from topic_tree_shallow")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="0-1 confidence this topic is answerable from this UI")
    rationale: str = Field("", description="Why this topic matches this screen (<=1 sentence)")
    expected_evidence: List[str] = Field(default_factory=list, description="What evidence to look for (short bullets)")


class TopicRouteResult(BaseModel):
    """
    LLM2-1 output: choose which questionnaire topics are relevant on this screen.
    """
    state_sig: str = Field(..., description="Echo input state_sig for staleness/debug")
    relevant_topics: List[RelevantTopic] = Field(default_factory=list, description="Relevant topics with confidence")
    skip_reason: str = Field("", description="If no relevant topics, why (short)")
    followups: List[str] = Field(default_factory=list, description="Optional followups like 'open Settings > Privacy'")


class RouterQuestionAnswer(BaseModel):
    """
    One router-question update inferred from the current screen.

    Notes:
      - This is still a questionnaire question, so question_id must be an existing router question id.
      - new_answer follows the same single/multi conventions as QuestionnaireUpdate.
    """
    question_id: str = Field(..., description="Router question id from router_questions input")
    new_answer: Union[str, List[str], None] = Field(
        None,
        description="For single-select use ONE option id/value string; for multi-select use a list of option ids/values.",
    )
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="0-1 confidence for this router answer")
    rationale: str = Field("", description="Short justification from the current UI (<=1 sentence)")


class RouterResult(BaseModel):
    """
    New LLM2-1 output for the router-only execution view.
    """
    state_sig: str = Field(..., description="Echo input state_sig for staleness/debug")
    router_updates: List[RouterQuestionAnswer] = Field(
        default_factory=list,
        description="Router questions that can be answered from the current screen",
    )


class AppMetadataSummary(BaseModel):
    """
    One-shot summary generated from app-level metadata (store description/category/etc.).
    This output is intended to be reused as stable context in later LLM calls.
    """
    app_id: str = Field("", description="App package id (e.g., com.example.app)")
    app_intro: str = Field("", description="One-sentence app overview inferred from description fields")
    focus_hints: str = Field(
        "",
        description=(
            "Potential content focus hints for UI review, written as short phrases separated by semicolons. "
            "Leave empty when evidence is insufficient."
        ),
    )
    questionnaire_type: Literal["games", "social", "others", ""] = Field(
        "",
        description="Questionnaire bucket inferred from genre fields. Empty when insufficient evidence.",
    )
    notes: str = Field(
        "",
        description="Brief reason when any output field is empty (e.g., missing source field, empty value, garbled text).",
    )


def _safe_json_from_text(text: str) -> Dict[str, Any]:
    if not text:
        raise ValueError("Empty model output")
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError(f"No JSON object found in: {text[:200]!r}")
    blob = re.sub(r",\s*([}\]])", r"\1", m.group(0))
    return json.loads(blob)


def _b64_image_url(b64: str) -> str:
    return f"data:image/png;base64,{b64}"


def _compact_digest(ui_json: Dict[str, Any], limit: int = 220) -> Dict[str, Any]:
    """
    Compact UI for LLM. Keep fields stable and small.
    NOTE: ids must stay aligned with BaseUI post-processing so NavigationProposal can reference them.
    """
    out: List[Dict[str, Any]] = []

    def walk(n: Dict[str, Any], depth: int = 0, parent_id: Optional[int] = None):
        if len(out) >= limit:
            return
        f = n.get("absolute_frame") or n.get("frame") or {}
        label = n.get("text") or n.get("content_desc") or n.get("semantic_label") or n.get("ocr_text") or n.get("icon_label")
        out.append(
            {
                "id": n.get("id"),
                "parent_id": parent_id,
                "depth": depth,
                "label": (str(label)[:90] if label else None),
                "text": (str(n.get("text"))[:90] if n.get("text") else None),
                "content_desc": (str(n.get("content_desc"))[:90] if n.get("content_desc") else None),
                "resource_id": (str(n.get("resource_id"))[:90] if n.get("resource_id") else None),
                "semantic_label": (str(n.get("semantic_label"))[:90] if n.get("semantic_label") else None),
                "semantic_type": (str(n.get("semantic_type"))[:40] if n.get("semantic_type") else None),
                "ocr_text": (str(n.get("ocr_text"))[:90] if n.get("ocr_text") else None),
                "icon_label": (str(n.get("icon_label"))[:60] if n.get("icon_label") else None),
                "clickable": bool(n.get("clickable")),
                "enabled": bool(n.get("enabled", True)),
                "bounds": [
                    int(f.get("x", 0)),
                    int(f.get("y", 0)),
                    int(f.get("width", 0)),
                    int(f.get("height", 0)),
                ],
                "class": (str(n.get("class") or "")[:60] or None),
            }
        )
        for ch in (n.get("subviews") or []):
            walk(ch, depth + 1, n.get("id"))

    for r in (ui_json.get("elements") or []):
        walk(r, 0, None)

    return {"elements": out, "screenscale": ui_json.get("screenscale", 1.0)}


_NAV_SYSTEM = """You are LLM1 for an Android UI exploration agent.

PRIMARY GOAL:
- Explore the app to gather evidence for a fixed questionnaire efficiently.
- Use block_status as the questionnaire state signal. It tells you which questionnaire
  blocks have been hit/visited so far; it is not a list of open questions.

INPUTS:
- state_sig: current UI signature (used to detect stale plans)
- task: exploration goal string
- block_status: mapping of block_id -> {topic?, hit_count, visit_count, module?}
- ui_digest: structured UI nodes with stable ids (YOU MUST reference these ids)
- screenshot: actual rendered screen image
- history: recent executed actions (context only)

REQUIRED OUTPUT (strict JSON matching NavigationProposal):
1) ui_view (UIView): concise screen interpretation for debugging.
   - action_elements: up to 12
   - hint_elements: up to 8
   - Use ids from ui_digest. If unsure, omit rather than hallucinate.

2) PAGE SEMANTICS (REUSABLE):
   - ui_type: stable page type label (e.g., settings_list, detail_form, auth_flow, modal_dialog, feed, search, subscription_paywall, permissions_dialog, webview, game_canvas, unknown)
   - page_tags: 0..10 TagSignal items bridging navigation to questionnaire topics (e.g., privacy, permissions, billing, account, help, legal, navigation_tab, close_control)
   - tag_evidence: <= 1 sentence
   - candidate_actions[].tags: 0..6 TagSignal items for each candidate when applicable

3) OVERLAY POLICY (CRITICAL):
   - Choose overlay_kind from: none | dismiss | workflow | loading
   - If a dialog/popup/permission/ad/paywall/age gate blocks interaction:
     set overlay_kind=\"dismiss\" and provide overlay_dismiss_actions first (max 5).
   - Prefer SAFE dismiss/deny/close/cancel/skip unless task requires acceptance.
   - When overlay_kind=\"dismiss\", candidate_actions should be empty or minimal.
   - If the \"overlay\" is actually a workflow surface (login/search/filter/form/otp),
     set overlay_kind=\"workflow\" and continue to propose candidate_actions normally.
   - If it's a transient spinner/transition, set overlay_kind=\"loading\".

4) EXPLORATION POLICY:
   - Provide 2-10 candidate_actions prioritised.
   - Each candidate should include 1-3 ordered actions (e.g., input then tap Confirm).
   - Provide a per-candidate \"score\" in [-1, 1] to express priority:
       * +1 strongly preferred, 0 neutral, -1 strongly deprioritized (e.g., go back).
   - Each reasoning should mention which page evidence or block_status coverage signal the action may help.

5) PROBE-RETURN POLICY (CRITICAL):
   - After a probe click, the agent may need to return to the original state to probe the next candidate.
   - Android BACK is NOT always safe (tabs, nested frames, webviews).
   - For EACH candidate:
       * If BACK might exit the page (especially tabs), set that candidate's return_method to \"custom\" or \"tab-back\"
         AND provide return_actions (1-4 steps) to restore the original state (e.g., click the original tab id).
   - Global return_method/return_actions are only defaults when a candidate leaves them empty.

6) DO NOT:
   - invent element_ids not in ui_digest
   - spam random clicks
   - leave the app intentionally (external links) unless clearly needed for questionnaire evidence
"""
#prompt调整

_Q_SYSTEM = """You are LLM2 for questionnaire filling based on CURRENT SCREEN evidence.

PRIMARY GOAL:
- Update only the MOST relevant questionnaire parts using evidence on this screen.
- Use hierarchy to infer parent/child relations when confident.

ANTI FLIP-FLOP (CRITICAL):
- Never set "No" just because evidence isn't visible on this screen.
- Do NOT flip prior Yes->No or remove multi-selections unless explicit contradiction exists AND confidence >= 0.95.

OUTPUT (strict JSON matching QuestionnaireUpdate):
- proposed_updates: minimal, evidence-backed changes
- For single-select questions: new_answer should be ONE option id/value (string), not a list.
- For multi-select questions: new_answer should be a list of option ids/values.
- evidence_refs must include ui ids when possible, e.g. {"ui_id": 12, "label": "In-app purchases"}
"""


_TOPIC_ROUTE_SYSTEM = """You are LLM2-1 (Topic Router) for an Android UI exploration agent.

GOAL:
- Decide which questionnaire TOPICS are relevant/answerable from this screen, cheaply.
- Do NOT attempt to answer questions here; only route topics.

INPUTS:
- state_sig: UI signature for staleness/debug
- topic_tree_shallow: list of topic entries {topic_id,title,keywords?,example_questions?}
- page_signals: small page cues (ocr_top_lines, ui_type?, local_tags?, page_summary?)

OUTPUT (strict JSON matching TopicRouteResult):
- relevant_topics: 0..8 topics, each with:
  - topic_id (must exist in topic_tree_shallow)
  - confidence 0..1
  - rationale (<=1 sentence)
  - expected_evidence (0..4 short strings)
- If none, set skip_reason (short).
- followups: optional suggestions to navigate to evidence surfaces (0..4).

RULES:
- Prefer precision over recall. If unsure, omit.
- Do not invent topic_ids.
"""

# 新的LLM2-1
_ROUTER_SYSTEM = """You are LLM2-1 (Router Filler) for an Android UI exploration agent.

GOAL:
- Answer only the provided router questions when the current screen gives clear evidence.

INPUTS:
- state_sig: UI signature for staleness/debug
- router_questions: router question entries:
  {
    id,
    full_id,
    module,
    category,
    question,
    type,
    options,
    show_if?
  }
- screenshot: the current UI screenshot; this is the primary evidence source

OUTPUT (strict JSON matching RouterResult):
- router_updates: 0..12 router answers, each with:
  - question_id (prefer full_id from router_questions; local id is acceptable only if full_id is absent)
  - new_answer
  - confidence
  - rationale(<=1 sentence)

RULES:
- Prefer precision over recall. If unsure, omit.
- Do not invent question_ids.
- If the current screen does not contain enough evidence for a router question, skip that question and do not include it in router_updates.
- Router questions are provided as a flat list. Treat `show_if` only as background context, not as an availability gate.
- For multiple-choice routers, `new_answer` should be a list of option ids.
- For single-choice routers, `new_answer` should be one option id string.
"""

_APP_METADATA_SYSTEM = """You are an assistant that summarizes Android app metadata for downstream UI analysis.

GOAL:
- Convert selected app metadata fields into a compact reusable context for later app UI exploration and UI analysis.
- Keep the output factual and concise; do not invent details not supported by the metadata.

INPUTS:
- app_id:
  - Android package id (unique app identifier).
- app_metadata: contains only the following selected fields:
  - description
    - Main app-store description text; primary source for app functionality/content.
  - descriptionHTML
    - HTML-formatted description text; may overlap with description and include markup artifacts.
  - summary
    - Short app tagline/summary of core purpose.
  - contentRating
    - Store-provided content-rating label.
  - contentRatingDescription
    - Optional explanation text for the content-rating decision.
  - offersIAP
    - Whether the app provides in-app purchases.
  - inAppProductPrice
    - In-app purchase price range or price note.
  - genre
    - Human-readable app category.
  - genreId
    - Normalized category id from store taxonomy.
  - categories
    - Category list payload (often serialized list/dict string).

WORKFLOW:
1) Build `app_intro` (based on description / descriptionHTML / summary).
   - Output one concise sentence that briefly introduces the app and provides hints for subsequent app exploration and UI analysis.
   - Do not mention the app's specific name; refer to it as "the app".

2) Build `focus_hints` (primarily based on contentRating / contentRatingDescription / offersIAP / inAppProductPrice, and also referencing description / descriptionHTML / summary).
   - Output short natural-language review hints for downstream UI inspection, summarizing what types of content in the app may affect age-related content considerations.
   - Format as semicolon-separated phrases, where each phrase represents one aspect.
   - IMPORTANT: do NOT explicitly output the app's age rating; only describe the related content.

3) Infer `questionnaire_type` (based on genre / genreId / categories).
   - Based on the app's category/type information, determine which questionnaire type applies.
   - The output must be exactly one of: games | social_apps | others.

4) Fill `notes`.
   - If any of the above three fields is empty, briefly explain why:
     e.g., missing source field, empty source value, garbled/unusable text, or insufficient evidence.
   - If all fields are confidently filled, `notes` should be empty.

OUTPUT (strict JSON matching AppMetadataSummary):
- app_id
- app_intro
- focus_hints
- questionnaire_type
- notes

RULES:
- Use only provided information; no guessing.
- Follow output format strictly.
"""


_TOPIC_FILL_SYSTEM = """You are LLM2-2 (Topic Filler) for an Android UI exploration agent.

GOAL:
- Propose questionnaire updates ONLY for the provided question_pack (topic-scoped).
- Use ONLY evidence present on the current screen (UI digest + screenshot).

ANTI FLIP-FLOP:
- Never set "No" just because evidence is not visible.
- Do not downgrade previous Yes->No or remove multi-select choices unless explicit contradiction is present.

INPUTS:
- topic_id: current topic being filled
- question_pack: list of questions (id,type,options,parents,children,current_answer,evidence_summary)
- current_answers: existing answers for those ids
- memory: topic-scoped memory (high confidence items)
- ui_digest + screenshot: authoritative current UI

OUTPUT (strict JSON matching QuestionnaireUpdate):
- proposed_updates: only include items you can justify from this screen.
- For single-select questions: new_answer should be ONE option id/value (string).
- For multi-select questions: new_answer should be a list of option ids/values.
"""


_BLOCK_FILL_SYSTEM = """You are LLM2-2 (Block Filler) for an Android UI exploration agent.

GOAL:
- Propose questionnaire updates ONLY for the provided block payload.
- Use ONLY evidence present on the current screen (screenshot + block questions).
- Skip any question that does not have enough visible evidence on this screen.

INPUTS:
- state_sig: UI signature for staleness/debug
- block_payload:
  {
    id,
    module,
    topic,
    block_show_if,
    questions: {
      question_id: {
        category,
        question,
        type,
        options,
        show_if
      }
    }
  }
- screenshot: current UI screenshot; this is the primary evidence source

OUTPUT (strict JSON matching QuestionnaireUpdate):
- proposed_updates: only include updates justified by this screen.
- For single-select questions: new_answer should be ONE option id/value string.
- For multi-select questions: new_answer should be a list of option ids/values.

RULES:
- Do not answer questions outside the given block.
- Prefer precision over recall. If unsure, omit.
- Never answer "No" only because evidence is absent.
- Use question ids from `block_payload.questions` as `question_id` in proposed_updates.
- Category hints for future merge:
  - mutual: options are different meanings; answer only the visibly supported option.
  - severity: options imply severity; answer the visibly supported severity.
  - frequency: answer only when the screenshot clearly supports a frequency/yes-no observation.
  - multiple_class: select all visibly supported classes.
  - multiple_freq: select all visibly supported frequency-tracked options.
"""


_BLOCKS_FILL_SYSTEM = """You are LLM2-2 (Blocks Filler) for an Android UI exploration agent.

GOAL:
- Fill the provided matched questionnaire blocks for the CURRENT screenshot.
- Use ONLY visible evidence in this screenshot.
- Skip questions that cannot be answered from the current screenshot.

INPUTS:
- state_sig: UI signature for debugging
- blocks: list of block payloads:
  {
    id,
    module,
    topic,
    block_show_if,
    questions: {
      question_id: {
        category,
        question,
        type,
        options,
        show_if
      }
    }
  }
- screenshot: current UI screenshot

OUTPUT (strict JSON matching BlocksFillResult):
- block_results: one item per block that has answerable questions:
  - block_id: must be one of the provided block ids
  - detected_signals: short evidence strings
  - proposed_updates: only questions inside that block
  - conflicts: optional notes

RULES:
- Do not answer questions outside the provided blocks.
- Use question ids exactly as keys in each block's `questions`.
- For single-select questions: new_answer should be one option id string.
- For multi-select questions: new_answer should be a list of option ids.
- Prefer precision over recall. If unsure, omit.
- Never answer "No" only because evidence is absent.
- Respect internal question show_if: answer a child only when its parent answer is supported in the same block.
- Category hints:
  - mutual: choose the visible matching meaning.
  - severity: choose the visible supported severity.
  - frequency: answer only when the screenshot clearly supports the yes/frequency observation.
  - multiple_class: select all visible classes.
  - multiple_freq: select visible frequency-tracked options.
"""


_RECOVERY_SYSTEM = """You are LLM3 for recovery in Android UI automation.

PRIMARY GOAL:
- Resolve popups/dialogs/ads/permission prompts
- Escape stuck states (no UI change, wrong page, return failed)
- Restore a stable exploration state

OUTPUT (strict JSON matching RecoveryProposal):
- ui_view required (concise)
- candidate_actions <= 5
- Allowed actions: click, input, back, wait, restart, none, complete
- Choose overlay_kind from: none | dismiss | workflow | loading

STRATEGY:
1) If overlay likely, propose click actions on Close/X/Cancel/Deny/Not now/OK (safe first).
2) If return failed, propose actions to return to stable state (close dialog, back, or click top-left back icon).
3) If truly stuck, propose restart as last resort.
"""


class GPTClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o",
        temperature: float = 0.2,
        timeout_s: int = 60,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.temperature = float(temperature)
        self.timeout_s = int(timeout_s)

        self.client = None
        try:
            import openai  # type: ignore

            if hasattr(openai, "OpenAI"):
                self.client = openai.OpenAI(api_key=self.api_key)
            elif hasattr(openai, "Client"):
                self.client = openai.Client(api_key=self.api_key) if self.api_key else openai.Client()
            else:
                self.client = openai
        except Exception:
            self.client = None

    @time_consumed
    def propose_navigation(
        self,
        screenshot_b64: str,
        ui_json: Dict[str, Any],
        block_status: Dict[str, Any],
        task: str,
        history: Optional[List[str]] = None,
        state_sig: str = "",
    ) -> NavigationProposal:
        """
        Ask LLM1 to interpret the current UI and propose navigation actions.

        Input:
        - screenshot_b64: current UI screenshot.
        - ui_json: compacted UI tree source.
        - block_status: questionnaire block runtime status; this is the
          navigation hint for questionnaire coverage.
        - task/history/state_sig: exploration goal and recent context.

        Processing:
        - Build a small UI digest.
        - Send block_status as questionnaire state context.

        Output:
        - NavigationProposal with overlay handling and candidate actions.
        """
        ui_digest = _compact_digest(ui_json, limit=240)
        payload = {
            "state_sig": state_sig,
            "task": task,
            "block_status": block_status,
            "history": (history or [])[-12:],
            "ui_digest": ui_digest,
        }

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _NAV_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": _b64_image_url(screenshot_b64)}} if screenshot_b64 else {"type": "text", "text": "(no screenshot)"},
                ],
            },
        ]

        out = self._call_structured(messages, NavigationProposal, opname="propose_navigation")
        out.overlay_dismiss_actions = list(out.overlay_dismiss_actions or [])[:5]
        out.candidate_actions = list(out.candidate_actions or [])[:10]
        for c in out.candidate_actions:
            c.actions = list(c.actions or [])[:3]
            c.return_actions = list(c.return_actions or [])[:4]
            c.tags = list(c.tags or [])[:6]
        out.key_interactables = list(out.key_interactables or [])[:12]
        out.return_actions = list(out.return_actions or [])[:4]
        out.page_tags = list(out.page_tags or [])[:10]
        out.state_sig = state_sig or out.state_sig
        return out

    @time_consumed
    def propose_questionnaire_updates(
        self,
        screenshot_b64: str,
        ui_json: Dict[str, Any],
        open_gaps: List[str],
        hierarchy: Any,
        memory: Optional[List[Dict[str, Any]]] = None,
        current_answers: Optional[Dict[str, Any]] = None,
    ) -> QuestionnaireUpdate:
        ui_digest = _compact_digest(ui_json, limit=260)
        payload = {
            "open_gaps": open_gaps[:120],
            "hierarchy": hierarchy,
            "memory": memory or [],
            "current_answers": current_answers or {},
            "ui_digest": ui_digest,
        }

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _Q_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": _b64_image_url(screenshot_b64)}} if screenshot_b64 else {"type": "text", "text": "(no screenshot)"},
                ],
            },
        ]

        out = self._call_structured(messages, QuestionnaireUpdate, opname="propose_questionnaire_updates")
        out.proposed_updates = list(out.proposed_updates or [])[:24]
        return out

    @time_consumed
    def propose_topic_routes(
        self,
        topic_tree_shallow: List[Dict[str, Any]],
        page_signals: Dict[str, Any],
        state_sig: str = "",
    ) -> TopicRouteResult:
        payload = {
            "state_sig": state_sig,
            "topic_tree_shallow": topic_tree_shallow[:80],
            "page_signals": page_signals,
        }

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _TOPIC_ROUTE_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        out = self._call_structured(messages, TopicRouteResult, opname="propose_topic_routes")
        out.relevant_topics = list(out.relevant_topics or [])[:8]
        for t in out.relevant_topics:
            t.expected_evidence = list(t.expected_evidence or [])[:4]
        out.followups = list(out.followups or [])[:4]
        out.state_sig = state_sig or out.state_sig
        return out

    @time_consumed
    def propose_topic_fill(
        self,
        screenshot_b64: str,
        ui_json: Dict[str, Any],
        topic_id: str,
        question_pack: List[Dict[str, Any]],
        memory: Optional[List[Dict[str, Any]]] = None,
        current_answers: Optional[Dict[str, Any]] = None,
        page_signals: Optional[Dict[str, Any]] = None,
        state_sig: str = "",
    ) -> QuestionnaireUpdate:
        ui_digest = _compact_digest(ui_json, limit=280)
        payload = {
            "state_sig": state_sig,
            "topic_id": topic_id,
            "page_signals": page_signals or {},
            "question_pack": question_pack[:28],
            "current_answers": current_answers or {},
            "memory": memory or [],
            "ui_digest": ui_digest,
        }

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _TOPIC_FILL_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": _b64_image_url(screenshot_b64)}} if screenshot_b64 else {"type": "text", "text": "(no screenshot)"},
                ],
            },
        ]

        out = self._call_structured(messages, QuestionnaireUpdate, opname="propose_topic_fill")
        out.proposed_updates = list(out.proposed_updates or [])[:24]
        return out

    @time_consumed
    def propose_router_answers(
        self,
        screenshot_b64: str,
        router_questions: List[Dict[str, Any]],
        state_sig: str = "",
    ) -> RouterResult:
        """
        New LLM2-1 for the router-only execution view.

        Inputs:
        - screenshot_b64: current screen screenshot, primary evidence source
        - router_questions:
          Executable router questions extracted from `questionnaire_routers.json`.
          Each router keeps the UI-level question shape plus:
          `id`, `full_id`, and `module`.
        - state_sig: current page state signature

        Output:
        - RouterResult:
          - router_updates: only the router questions answerable on this screen
        """
        payload = {
            "state_sig": state_sig,
            "router_questions": router_questions[:40],
        }

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _ROUTER_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": _b64_image_url(screenshot_b64)}} if screenshot_b64 else {"type": "text", "text": "(no screenshot)"},
                ],
            },
        ]

        out = self._call_structured(messages, RouterResult, opname="propose_router_answers")
        out.router_updates = list(out.router_updates or [])[:12]
        out.state_sig = state_sig or out.state_sig
        return out

    @time_consumed
    def analyze_app_metadata(
        self,
        app_id: str,
        app_metadata: Dict[str, Any],
    ) -> AppMetadataSummary:
        """
        Analyze selected app metadata fields and return a compact reusable summary.

        Inputs:
        - app_id:
          App package id, usually from CSV column `appId`.
        - app_metadata:
          Selected metadata fields only (description/summary/rating/iap/genre related).

        Processing:
        - Keep payload compact (trim long string fields).
        - Ask structured LLM with AppMetadataSummary schema.

        Output:
        - AppMetadataSummary:
          app_intro + focus_hints + questionnaire_type (+ notes for empty fields).
        """
        compact_meta: Dict[str, Any] = {}
        for k, v in (app_metadata or {}).items():
            key = str(k or "").strip()
            if not key:
                continue
            if v is None:
                compact_meta[key] = ""
                continue
            s = str(v)
            compact_meta[key] = s[:4000] if len(s) > 4000 else s

        payload = {
            "app_id": app_id,
            "app_metadata": compact_meta,
        }
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _APP_METADATA_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        out = self._call_structured(messages, AppMetadataSummary, opname="analyze_app_metadata")
        out.app_id = app_id or out.app_id
        out.app_intro = str(out.app_intro or "")[:280]
        out.focus_hints = str(out.focus_hints or "")[:500]
        out.notes = str(out.notes or "")[:500]
        if out.questionnaire_type not in ("games", "social", "others", ""):
            out.questionnaire_type = ""
        return out

    @time_consumed
    def propose_block_fill(
        self,
        screenshot_b64: str,
        block_payload: Dict[str, Any],
        state_sig: str = "",
    ) -> QuestionnaireUpdate:
        """
        Fill one matched questionnaire block using the current screenshot.

        Inputs:
        - screenshot_b64:
          Base64-encoded screenshot for the current UI. This is the primary evidence source.
        - block_payload:
          The full payload of one block from `questionnaire_blocks.json`.
          Expected keys include:
          - id
          - module
          - topic
          - block_show_if
          - questions
        - state_sig:
          Current UI state signature for debugging/staleness.

        Processing:
        - Build a compact payload containing only this block and state_sig.
        - Send the payload and screenshot to the structured LLM call.
        - Trim the returned updates to a safe small size.

        Output:
        - QuestionnaireUpdate
          The `proposed_updates` field represents this block's answer candidates
          on the current screen.
        """
        payload = {
            "state_sig": state_sig,
            "block_payload": block_payload,
        }

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _BLOCK_FILL_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": _b64_image_url(screenshot_b64)}} if screenshot_b64 else {"type": "text", "text": "(no screenshot)"},
                ],
            },
        ]

        out = self._call_structured(messages, QuestionnaireUpdate, opname="propose_block_fill")
        out.proposed_updates = list(out.proposed_updates or [])[:24]
        return out

    @time_consumed
    def propose_blocks_fill(
        self,
        screenshot_b64: str,
        blocks_payload: List[Dict[str, Any]],
        state_sig: str = "",
    ) -> BlocksFillResult:
        """
        Fill all matched blocks for one UI state in a single LLM2-2 call.

        Input:
        - screenshot_b64: current UI screenshot.
        - blocks_payload: matched block payloads from QuestionnaireState2.
        - state_sig: current UI state signature.

        Processing:
        - Send the current screenshot and all matched blocks together.
        - The model returns results grouped by block_id.

        Output:
        - BlocksFillResult. Each block result contains proposed question updates
          for that block only.
        """
        payload = {
            "state_sig": state_sig,
            "blocks": blocks_payload,
        }

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _BLOCKS_FILL_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": _b64_image_url(screenshot_b64)}} if screenshot_b64 else {"type": "text", "text": "(no screenshot)"},
                ],
            },
        ]

        out = self._call_structured(messages, BlocksFillResult, opname="propose_blocks_fill")
        out.state_sig = state_sig or out.state_sig
        out.block_results = list(out.block_results or [])[:20]
        for block_result in out.block_results:
            block_result.proposed_updates = list(block_result.proposed_updates or [])[:24]
        return out

    @time_consumed
    def recover_state(
        self,
        screenshot_b64: str,
        ui_json: Dict[str, Any],
        frontier_hint: str = "",
        last_nav: Optional[NavigationProposal] = None,
        note: str = "",
        state_sig: str = "",
    ) -> RecoveryProposal:
        ui_digest = _compact_digest(ui_json, limit=240)
        payload = {
            "state_sig": state_sig,
            "frontier_hint": frontier_hint,
            "note": note,
            "last_nav": last_nav.model_dump() if last_nav else None,
            "ui_digest": ui_digest,
        }

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _RECOVERY_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": _b64_image_url(screenshot_b64)}} if screenshot_b64 else {"type": "text", "text": "(no screenshot)"},
                ],
            },
        ]

        out = self._call_structured(messages, RecoveryProposal, opname="recover_state")
        out.candidate_actions = list(out.candidate_actions or [])[:5]
        out.state_sig = state_sig or out.state_sig
        return out

    def _call_structured(self, messages: List[Dict[str, Any]], model_cls: Type[R], opname: str) -> R:
        """
        IPO:
          in : messages (system+user), model_cls (Pydantic), opname
          out: parsed Pydantic instance
        WHEN called:
          - internal helper for each LLM call
        FALLBACKS:
          - prefer beta.chat.completions.parse when available
          - otherwise use chat.completions.create and JSON-extract
        """
        if self.client is None:
            raise RuntimeError(f"OpenAI client not available for {opname}")

        start_ts = time.time()
        stage = self._llm_stage_from_opname(opname)
        state_sig = self._extract_state_sig(messages)

        # Preferred: structured parse
        try:
            resp = self.client.beta.chat.completions.parse(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                timeout=self.timeout_s,
                response_format=model_cls,
            )
            try:
                prompt_tokens = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(resp.usage, "completion_tokens", 0) or 0)
                token_record(opname, prompt_tokens, completion_tokens)
                self._log_llm_usage(
                    stage=stage,
                    opname=opname,
                    state_sig=state_sig,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    duration_s=(time.time() - start_ts),
                    path="parse",
                )
            except Exception:
                pass
            return resp.choices[0].message.parsed
        except Exception:
            logger.error("Structured parse failed for %s; falling back to JSON extraction.", opname, exc_info=True)

        # Fallback: normal completion (best-effort across OpenAI client versions)
        try:
            if hasattr(self.client, "chat") and hasattr(self.client.chat, "completions"):
                resp2 = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    timeout=self.timeout_s,
                )
                try:
                    prompt_tokens = int(getattr(resp2.usage, "prompt_tokens", 0) or 0)
                    completion_tokens = int(getattr(resp2.usage, "completion_tokens", 0) or 0)
                    token_record(opname, prompt_tokens, completion_tokens)
                    self._log_llm_usage(
                        stage=stage,
                        opname=opname,
                        state_sig=state_sig,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        duration_s=(time.time() - start_ts),
                        path="create",
                    )
                except Exception:
                    pass
                text = resp2.choices[0].message.content or ""
            elif hasattr(self.client, "ChatCompletion"):
                # legacy module-style client
                resp2 = self.client.ChatCompletion.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    timeout=self.timeout_s,
                )
                try:
                    usage = resp2.get("usage") or {}
                    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                    token_record(opname, prompt_tokens, completion_tokens)
                    self._log_llm_usage(
                        stage=stage,
                        opname=opname,
                        state_sig=state_sig,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        duration_s=(time.time() - start_ts),
                        path="legacy_create",
                    )
                except Exception:
                    pass
                text = ((resp2.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            else:
                raise RuntimeError("No supported chat completion method on client")

            data = _safe_json_from_text(text)
            if hasattr(model_cls, "model_validate"):
                return model_cls.model_validate(data)  # type: ignore[return-value]
            return model_cls.parse_obj(data)  # type: ignore[return-value]
        except Exception as exc:
            raise RuntimeError(f"LLM call failed for {opname}: {exc}") from exc

    @staticmethod
    def _llm_stage_from_opname(opname: str) -> str:
        if opname == "propose_navigation":
            return "LLM1"
        if opname in {
            "propose_questionnaire_updates",
            "propose_topic_routes",
            "propose_topic_fill",
            "propose_router_answers",
            "propose_block_fill",
            "propose_blocks_fill",
        }:
            return "LLM2"
        if opname == "recover_state":
            return "LLM3"
        return "LLM"

    @staticmethod
    def _extract_state_sig(messages: List[Dict[str, Any]]) -> str:
        for msg in reversed(messages or []):
            content = msg.get("content")
            try:
                if isinstance(content, str):
                    payload = json.loads(content)
                    sig = str((payload or {}).get("state_sig") or "").strip()
                    if sig:
                        return sig
                elif isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if str(item.get("type") or "") != "text":
                            continue
                        text = str(item.get("text") or "").strip()
                        if not text:
                            continue
                        payload = json.loads(text)
                        sig = str((payload or {}).get("state_sig") or "").strip()
                        if sig:
                            return sig
            except Exception:
                continue
        return ""

    @staticmethod
    def _log_llm_usage(
        *,
        stage: str,
        opname: str,
        state_sig: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration_s: float,
        path: str,
    ) -> None:
        total_tokens = int(prompt_tokens) + int(completion_tokens)
        sig8 = (state_sig or "")[:8]
        logger.info(
            "LLM_USAGE stage=%s op=%s sig=%s prompt=%d completion=%d total=%d duration_s=%.3f path=%s",
            stage,
            opname,
            sig8,
            int(prompt_tokens),
            int(completion_tokens),
            total_tokens,
            float(duration_s),
            path,
        )



__all__ = [
    "GPTClient",
    "UIView",
    "UIElement",
    "UIElementType",
    "ActionStep",
    "ActionCandidate",
    "ActionType",
    "OverlayKind",
    "NavigationProposal",
    "RecoveryProposal",
    "QuestionnaireUpdate",
    "TopicRouteResult",
    "RelevantTopic",
    "RouterResult",
    "RouterQuestionAnswer",
    "ProposedUpdate",
]
