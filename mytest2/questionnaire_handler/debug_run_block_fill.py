"""
debug_run_block_fill.py

Purpose:
- Call the new block filler (`propose_block_fill`) in isolation.
- Do NOT touch the main workflow.
- Load one block payload from `questionnaire_state2.py` and send it to the LLM.

Usage:
python mytest2\\questionnaire_handler\\debug_run_block_fill.py ^
  --questionnaire-dir questionnaire-v2\\games ^
  --screenshot path\\to\\screen.png ^
  --block-id controlled.controlled_substance_includes.alcohol

Note:
- `--questionnaire-dir` is only used to infer the generated split directory.
- The block payload is loaded from
  `mytest2/questionnaire_handler/chain_debug/<collection>_split/questionnaire_blocks.json`.
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
    parser.add_argument("--block-id", required=True, help="Block id to fill")
    parser.add_argument("--state-sig", default="debug_block_sig", help="Debug state signature")
    parser.add_argument("--model", default="gpt-4o", help="Model name")
    parser.add_argument("--temperature", type=float, default=0.2, help="LLM temperature")
    parser.add_argument("--timeout", type=int, default=60, help="LLM timeout seconds")
    parser.add_argument("--api-key", default=None, help="OpenAI API key; fallback to OPENAI_API_KEY")
    args = parser.parse_args()

    questionnaire_dir = str(args.questionnaire_dir).strip()
    screenshot_path = Path(str(args.screenshot).strip()).resolve()
    block_id = str(args.block_id).strip()
    state_sig = str(args.state_sig).strip()
    api_key = args.api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Set it in .env or pass --api-key.")

    qs = QuestionnaireState.load_from_questionnaire_dir(questionnaire_dir)
    block_payload = qs.get_block_payload(block_id)
    if block_payload is None:
        raise SystemExit(f"Block not found: {block_id}")

    screenshot_b64 = read_image_b64(screenshot_path)
    gpt = GPTClient(
        api_key=api_key,
        model=str(args.model),
        temperature=float(args.temperature),
        timeout_s=int(args.timeout),
    )

    update = gpt.propose_block_fill(
        screenshot_b64=screenshot_b64,
        block_payload=block_payload,
        state_sig=state_sig,
    )

    print(
        json.dumps(
            {
                "questionnaire_dir": questionnaire_dir,
                "screenshot": str(screenshot_path),
                "state_sig": state_sig,
                "block_id": block_id,
                "block_payload": block_payload,
                "block_fill_result": update.model_dump(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
