"""
debug_run_router_llm.py

Purpose:
- Call the new router LLM (`propose_router_answers`) in isolation.
- Do NOT touch the main workflow.
- After LLM returns router answers, locally match blocks using `questionnaire_state2.py`.

Usage:
python mytest2\\questionnaire_handler\\debug_run_router_llm.py ^
  --questionnaire-dir questionnaire-v2\\games ^
  --screenshot path\\to\\screen.png ^
  --state-sig debug_sig

Note:
- `--questionnaire-dir` is only used to infer the generated split directory.
- Router questions and matched blocks are loaded from
  `mytest2/questionnaire_handler/chain_debug/<collection>_split/`.
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
    - path: Path to a local screenshot image.

    Output:
    - str
      Base64-encoded image content.
    """
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questionnaire-dir", required=True, help="Original questionnaire dir, e.g. questionnaire-v2/games")
    parser.add_argument("--screenshot", required=True, help="Path to one screenshot image")
    parser.add_argument("--state-sig", default="debug_router_sig", help="Debug state signature")
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
    router_questions = qs.routers
    screenshot_b64 = read_image_b64(screenshot_path)

    gpt = GPTClient(
        api_key=api_key,
        model=str(args.model),
        temperature=float(args.temperature),
        timeout_s=int(args.timeout),
    )

    router_result = gpt.propose_router_answers(
        screenshot_b64=screenshot_b64,
        router_questions=router_questions,
        state_sig=state_sig,
    )

    router_updates = [item.model_dump() for item in (router_result.router_updates or [])]
    matched_blocks = qs.match_blocks_from_router_answers(router_updates)
    qs.mark_blocks_hit(matched_blocks)

    payload = {
        "questionnaire_dir": questionnaire_dir,
        "screenshot": str(screenshot_path),
        "state_sig": state_sig,
        "router_updates": router_updates,
        "matched_block_ids": [row.get("id") for row in matched_blocks],
    }

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
