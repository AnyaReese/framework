"""
debug_questionnaire_state2.py

Purpose:
- Validate the current `questionnaire_state2.py` helper functions without touching
  the main workflow.
- Show how routers, blocks, block_status, block matching, counter updates, and
  observation saving behave on hand-crafted router answers.

What this script demonstrates:
1. Load routers / blocks from a questionnaire directory.
2. Print the three core structures:
   - qs.routers
   - qs.blocks
   - qs.block_status
3. Run `match_blocks_from_router_answers(...)` using a small manual example.
4. Update hit counters with `mark_blocks_hit(...)`.
5. Fetch one block payload with `get_block_payload(...)`.
6. Save one observation JSON locally.

Usage:
python mytest2\\questionnaire_handler\\debug_questionnaire_state2.py --questionnaire-dir questionnaire-v2\\games

Important:
- `--questionnaire-dir` still points to the original collection only so the
  script can infer which generated split folder to load.
- The actual runtime data comes from:
  `mytest2/questionnaire_handler/chain_debug/<collection>_split/`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from questionnaire_state2 import QuestionnaireState


def build_demo_router_answers(qs: QuestionnaireState) -> list[dict]:
    """
    Build a tiny manual router-answer example for debugging.

    Input:
    - qs:
      A loaded QuestionnaireState containing routers.

    Processing:
    - Look for a few well-known games router questions.
    - If found, construct answers that should activate some obvious blocks.

    Output:
    - List[dict]
      Example:
      [
        {"question_id": "controlled.controlled_substance_includes", "new_answer": ["alcohol"]},
        {"question_id": "fear.scary_elements_includes", "new_answer": ["scary_elements"]},
      ]
    """
    answers: list[dict] = []
    known = {router["full_id"]: router for router in qs.routers}

    if "controlled.controlled_substance_includes" in known:
        answers.append(
            {
                "question_id": "controlled.controlled_substance_includes",
                "new_answer": ["alcohol"],
            }
        )
    if "fear.scary_elements_includes" in known:
        answers.append(
            {
                "question_id": "fear.scary_elements_includes",
                "new_answer": ["scary_elements"],
            }
        )
    if "language.language_includes" in known:
        answers.append(
            {
                "question_id": "language.language_includes",
                "new_answer": ["sexual_expletives_e_g_fuck_and_cunt"],
            }
        )
    return answers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--questionnaire-dir",
        default="questionnaire-v2/games",
        help="Path to the original questionnaire directory, e.g. questionnaire-v2/games",
    )
    args = parser.parse_args()

    questionnaire_dir = str(args.questionnaire_dir).strip()
    collection = Path(questionnaire_dir).resolve().name
    out_dir = PROJECT_ROOT / "mytest2" / "questionnaire_handler" / "chain_debug" / f"{collection}_state2_debug"
    obs_dir = out_dir / "observations"
    out_dir.mkdir(parents=True, exist_ok=True)

    qs = QuestionnaireState.load_from_questionnaire_dir(questionnaire_dir)

    demo_router_answers = build_demo_router_answers(qs)
    matched_blocks = qs.match_blocks_from_router_answers(demo_router_answers)
    qs.mark_blocks_hit(matched_blocks)

    first_block_payload = None
    if matched_blocks:
        first_block_id = str(matched_blocks[0].get("id") or "")
        first_block_payload = qs.get_block_payload(first_block_id)

    observation_path = qs.save_observation(
        out_dir=str(obs_dir),
        state_sig="debug_state_sig",
        router_answers=demo_router_answers,
        matched_blocks=matched_blocks,
        block_fill_results=[],
        screenshot_path="",
    )

    summary = {
        "questionnaire_dir": questionnaire_dir,
        "routers_count": len(qs.routers),
        "blocks_count": len(qs.blocks),
        "block_status_count": len(qs.block_status),
        "routers_sample": qs.routers[:3],
        "blocks_sample": qs.blocks[:3],
        "block_status_sample": list(qs.block_status.values())[:5],
        "demo_router_answers": demo_router_answers,
        "matched_block_ids": [row.get("id") for row in matched_blocks],
        "first_block_payload": first_block_payload,
        "observation_path": str(observation_path),
    }

    out_path = out_dir / "state2_debug_summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"questionnaire_dir={questionnaire_dir}")
    print(f"routers_count={len(qs.routers)}")
    print(f"blocks_count={len(qs.blocks)}")
    print(f"block_status_count={len(qs.block_status)}")
    print(f"saved={out_path}")
    print(f"observation_saved={observation_path}")
    print("demo_router_answers=")
    for row in demo_router_answers:
        print(json.dumps(row, ensure_ascii=False))
    print("matched_block_ids=")
    for row in matched_blocks[:10]:
        print(str(row.get("id") or ""))


if __name__ == "__main__":
    main()
