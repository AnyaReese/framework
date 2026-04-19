# 调试drift检测的代码,第一次按回车记录页面当做初始状态,第二次按回车触发drift检测判断页面是否变化并输出相关信息.

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from appium_android import AndroidAppiumClient
from gpt_cls import GPTClient
from questionnaire_state import QuestionnaireState
from trace_callbacks import NoOpCallbacks
from workflow import BudgetConfig, WorkflowRunner, compare_phash_similarity


load_dotenv()
OUTPUT_DIR = Path(__file__).resolve().parent / "preflight_refresh_test"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Debug _preflight_refresh_if_changed() timing.")
    p.add_argument("--appium-url", type=str, default="http://127.0.0.1:4723")
    p.add_argument("--device-name", type=str, default=None)
    p.add_argument("--package", type=str, required=True)
    p.add_argument("--activity", type=str, default=None)
    p.add_argument("--questionnaire-dir", type=str, required=True)
    p.add_argument("--model", type=str, default="gpt-4o")
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--api-key", type=str, default=None)
    p.add_argument("--time-budget", type=float, default=300.0)
    p.add_argument("--max-actions", type=int, default=300)
    p.add_argument("--probe-cap", type=int, default=10)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--timeout-s", type=float, default=0.8, help="timeout_s passed into _preflight_refresh_if_changed")
    p.add_argument("--repeat", type=int, default=1, help="how many times to time the function")
    return p.parse_args()


# ============================
package = "bim.app"

sys.argv = [
    sys.argv[0],
    "--appium-url", "http://127.0.0.1:4723",
    "--device-name", "emulator-5554",
    "--package", package,
    "--questionnaire-dir", "../questionnaire-v2/games",
    "--timeout-s", "0.8",
    "--repeat", "3",
]
# ============================


def build_runner(args: argparse.Namespace) -> tuple[WorkflowRunner, AndroidAppiumClient]:
    api_key = args.api_key or os.getenv("OPENAI_API_KEY", "")
    questionnaires = QuestionnaireState.load_from_dir(args.questionnaire_dir)
    appium = AndroidAppiumClient(server_url=args.appium_url, device_name=args.device_name).init_connection()
    gpt = GPTClient(api_key=api_key, model=args.model, temperature=args.temperature, timeout_s=args.timeout)
    budget = BudgetConfig(
        time_budget_s=float(args.time_budget),
        max_actions=int(args.max_actions),
        per_page_probe_cap=int(args.probe_cap),
        max_workers=int(args.workers),
    )
    runner = WorkflowRunner(
        appium=appium,
        gpt=gpt,
        questionnaires=questionnaires,
        budget=budget,
        target_package=args.package,
        target_activity=args.activity,
        pause=False,
        callbacks=NoOpCallbacks(),
    )
    return runner, appium


def _save_snap_artifacts(snap: dict, out_dir: Path, prefix: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{prefix}_snap.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if snap.get("xml"):
        (out_dir / f"{prefix}.xml").write_text(str(snap.get("xml") or ""), encoding="utf-8")
    if snap.get("xml_raw"):
        (out_dir / f"{prefix}_raw.xml").write_text(str(snap.get("xml_raw") or ""), encoding="utf-8")
    if snap.get("screenshot"):
        (out_dir / f"{prefix}.png").write_bytes(base64.b64decode(str(snap.get("screenshot") or "") + "=="))
    if snap.get("screenshot_raw"):
        (out_dir / f"{prefix}_raw.png").write_bytes(base64.b64decode(str(snap.get("screenshot_raw") or "") + "=="))


def main() -> int:
    args = parse_args()
    runner, appium = build_runner(args)
    try:
        print("=== preflight refresh timing ===")
        print(f"timeout_s={args.timeout_s}")
        run_dir = OUTPUT_DIR / time.strftime("%Y%m%d_%H%M%S")

        input("\n第一步：把页面停在你要作为基准的状态，然后按回车采集当前 snap...")
        snap = runner._capture_and_process()
        if not snap:
            print("基准 snap 生成失败，无法测试 _preflight_refresh_if_changed")
            return 1
        _save_snap_artifacts(snap, run_dir, "step1_base")

        print("\n=== 基准页面信息 ===")
        print(f"base_state_sig={snap.get('state_sig')}")
        print(f"base_xml_reliable={snap.get('xml_reliable')}")
        print(f"base_identity_source={(snap.get('meta') or {}).get('identity_source')}")
        print(f"base_screenshot_phash={(snap.get('meta') or {}).get('screenshot_phash')}")
        print(f"saved_base_dir={run_dir}")

        input("\n第二步：你现在可以手动保持页面不动，或者切到另一个状态。准备好后按回车开始判断页面是否变化...")
        snap_step2 = runner._capture_and_process()
        phash_similarity = None
        if snap_step2:
            _save_snap_artifacts(snap_step2, run_dir, "step2_current")
            print(f"step2_state_sig={snap_step2.get('state_sig')}")
            print(f"step2_xml_reliable={snap_step2.get('xml_reliable')}")
            print(f"step2_identity_source={(snap_step2.get('meta') or {}).get('identity_source')}")
            print(f"step2_screenshot_phash={(snap_step2.get('meta') or {}).get('screenshot_phash')}")
            base_phash = str((snap.get("meta") or {}).get("screenshot_phash") or "")
            step2_phash = str((snap_step2.get("meta") or {}).get("screenshot_phash") or "")
            if base_phash and step2_phash:
                phash_similarity = compare_phash_similarity(base_phash, step2_phash)
                print(f"step1_vs_step2_phash_similarity={phash_similarity:.4f}")
                print(f"phash_similarity_threshold={runner.budget.screenshot_phash_similarity_threshold:.4f}")
        else:
            print("第二步页面采集失败，未能保存 step2_current 产物")

        t0 = time.perf_counter()
        refreshed = runner._preflight_refresh_if_changed(snap, timeout_s=float(args.timeout_s))
        elapsed = time.perf_counter() - t0

        print("\n=== 检测结果 ===")
        print(f"elapsed_s={elapsed:.4f}")
        if refreshed is None:
            print("result=unchanged")
            print(f"current_state_sig(still_base)={snap.get('state_sig')}")
        else:
            print("result=refreshed")
            print(f"new_state_sig={refreshed.get('state_sig')}")
            print(f"new_xml_reliable={refreshed.get('xml_reliable')}")
            print(f"new_identity_source={(refreshed.get('meta') or {}).get('identity_source')}")
            print(f"new_screenshot_phash={(refreshed.get('meta') or {}).get('screenshot_phash')}")
            _save_snap_artifacts(refreshed, run_dir, "step2_refreshed")

        if phash_similarity is not None:
            print(f"printed_step1_vs_step2_phash_similarity={phash_similarity:.4f}")
        print(f"saved_test_dir={run_dir}")

        input("\n按回车结束...")
        return 0
    finally:
        appium.quit()


if __name__ == "__main__":
    raise SystemExit(main())
