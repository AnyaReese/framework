"""
debug_router_block_pipeline.py

Purpose:
- Run the new router -> local block matching -> block fill chain end-to-end
  in a debug script without modifying the main workflow.

What this script does:
1. Load `questionnaire_state2.py`.
2. Call the router LLM on one screenshot.
3. Match blocks locally from router answers.
4. Fill every matched block with the block filler LLM.
5. Update block counters.
6. Save one observation JSON.

Usage:
python mytest2\\questionnaire_handler\\debug_router_block_pipeline.py ^
  --questionnaire-dir questionnaire-v2\\games ^
  --screenshot path\\to\\screen.png

Note:
- This debug script intentionally fills all matched blocks. There is no block
  limit, so `matched_block_ids` and `block_fill_results` should line up unless
  an individual block payload is missing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from gpt_cls import GPTClient
from questionnaire_state2 import QuestionnaireState

load_dotenv(PROJECT_ROOT / ".env")


def read_image_b64(path: Path) -> str:
    """
    Read one local image file and return base64 text.

    Input:
    - path: local screenshot path.

    Output:
    - str: base64 image content for `GPTClient`.
    """
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questionnaire-dir", required=True, help="Original questionnaire dir, e.g. questionnaire-v2/games")
    parser.add_argument("--screenshot", required=True, help="Path to one screenshot image")
    parser.add_argument("--state-sig", default="debug_pipeline_sig", help="Debug state signature")
    parser.add_argument("--model", default="gpt-4o", help="Model name")
    parser.add_argument("--temperature", type=float, default=0.2, help="LLM temperature")
    parser.add_argument("--timeout", type=int, default=60, help="LLM timeout seconds")
    parser.add_argument("--api-key", default=None, help="OpenAI API key; fallback to OPENAI_API_KEY")
    args = parser.parse_args()

    questionnaire_dir = str(args.questionnaire_dir).strip()
    screenshot_path = Path(str(args.screenshot).strip()).resolve()
    state_sig = str(args.state_sig).strip()
    api_key = args.api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Set it in .env or pass --api-key.")

    qs = QuestionnaireState.load_from_questionnaire_dir(questionnaire_dir)
    screenshot_b64 = read_image_b64(screenshot_path)
    gpt = GPTClient(
        api_key=api_key,
        model=str(args.model),
        temperature=float(args.temperature),
        timeout_s=int(args.timeout),
    )

    router_result = gpt.propose_router_answers(
        screenshot_b64=screenshot_b64,
        router_questions=qs.routers,
        state_sig=state_sig,
    )
    router_updates = [item.model_dump() for item in (router_result.router_updates or [])]

    matched_blocks = qs.match_blocks_from_router_answers(router_updates)
    qs.mark_blocks_hit(matched_blocks)

    visited_ids = [str(block.get("id") or "") for block in matched_blocks]
    qs.mark_blocks_visited(visited_ids)

    block_fill_results = []
    for block in matched_blocks:
        block_payload = qs.get_block_payload(str(block.get("id") or ""))
        if block_payload is None:
            continue
        result = gpt.propose_block_fill(
            screenshot_b64=screenshot_b64,
            block_payload=block_payload,
            state_sig=state_sig,
        )
        block_fill_results.append(
            {
                "block_id": block_payload.get("id"),
                "proposed_updates": [row.model_dump() for row in (result.proposed_updates or [])],
                "raw_result": result.model_dump(),
            }
        )

    out_dir = PROJECT_ROOT / "mytest2" / "questionnaire_handler" / "chain_debug" / f"{Path(questionnaire_dir).resolve().name}_state2_debug"
    observation_path = qs.save_observation(
        out_dir=str(out_dir / "observations"),
        state_sig=state_sig,
        router_answers=router_updates,
        matched_blocks=matched_blocks,
        block_fill_results=block_fill_results,
        screenshot_path=str(screenshot_path),
    )

    print(
        json.dumps(
            {
                "questionnaire_dir": questionnaire_dir,
                "screenshot": str(screenshot_path),
                "state_sig": state_sig,
                "router_updates": router_updates,
                "matched_block_ids": [row.get("id") for row in matched_blocks],
                "visited_block_ids": visited_ids,
                "block_fill_results": block_fill_results,
                "observation_path": str(observation_path),
                "block_status": qs.block_status,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
