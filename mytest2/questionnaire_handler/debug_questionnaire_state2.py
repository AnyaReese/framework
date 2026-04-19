"""
debug_questionnaire_state2.py

Purpose:
- Validate the minimal loader in `questionnaire_state2.py`.
- Show the three current runtime structures clearly:
  - routers
  - blocks
  - block_status

Usage:
python mytest2\\questionnaire_handler\\debug_questionnaire_state2.py --questionnaire-dir questionnaire-v2\\games
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
    out_dir.mkdir(parents=True, exist_ok=True)

    qs = QuestionnaireState.load_from_questionnaire_dir(questionnaire_dir)
    # print(qs.routers)
    print(qs.blocks)
    # print(qs.block_status)
    # summary = {
    #     "questionnaire_dir": questionnaire_dir,
    #     "routers_count": len(qs.routers),
    #     "blocks_count": len(qs.blocks),
    #     "block_status_count": len(qs.block_status),
    #     "routers_sample": qs.routers[:5],
    #     "blocks_sample": qs.blocks[:5],
    #     "block_status_sample": list(qs.block_status.values())[:5],
    # }

    # out_path = out_dir / "state2_structures.json"
    # out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # print(f"questionnaire_dir={questionnaire_dir}")
    # print(f"routers_count={len(qs.routers)}")
    # print(f"blocks_count={len(qs.blocks)}")
    # print(f"block_status_count={len(qs.block_status)}")
    # print(f"saved={out_path}")

    # print("routers_sample=")
    # for row in qs.routers[:3]:
    #     print(json.dumps(row, ensure_ascii=False))

    # print("blocks_sample=")
    # for row in qs.blocks[:3]:
    #     print(json.dumps(row, ensure_ascii=False))

    # print("block_status_sample=")
    # for row in list(qs.block_status.values())[:3]:
    #     print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
