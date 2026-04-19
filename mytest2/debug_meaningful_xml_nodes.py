from __future__ import annotations

import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from appium_android import AndroidAppiumClient
from gpt_cls import GPTClient
from questionnaire_state import QuestionnaireState
from trace_callbacks import NoOpCallbacks
from workflow import BudgetConfig, WorkflowRunner


load_dotenv()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Debug meaningful XML node count.")
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
    p.add_argument("--threshold", type=int, default=2, help="meaningful node count below this is treated as unreliable")
    return p.parse_args()


# ============================
# package = "bim.app"
# package = "bim.app"
package = "com.android.settings"
sys.argv = [
    sys.argv[0],
    "--appium-url", "http://127.0.0.1:4723",
    "--device-name", "emulator-5554",
    "--package", package,
    "--questionnaire-dir", "../questionnaire-v2/games",
    "--threshold", "2",
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


def count_meaningful_xml_nodes(xml_text: str) -> int:
    if not xml_text or "<hierarchy" not in xml_text:
        return 0
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return 0

    screen_w = int(root.attrib.get("width", "0") or 0)
    screen_h = int(root.attrib.get("height", "0") or 0)
    count = 0

    for node in root.iter():
        if node.tag == "hierarchy":
            continue

        bounds_text = str(node.attrib.get("bounds", "") or "").strip()
        bounds: tuple[int, int, int, int] | None = None
        if bounds_text.startswith("[") and "][" in bounds_text and bounds_text.endswith("]"):
            try:
                left, right = bounds_text[1:-1].split("][", 1)
                x1_s, y1_s = left.split(",", 1)
                x2_s, y2_s = right.split(",", 1)
                x1, y1, x2, y2 = int(x1_s), int(y1_s), int(x2_s), int(y2_s)
                if x2 > x1 and y2 > y1:
                    bounds = (x1, y1, x2, y2)
            except Exception:
                bounds = None

        if bounds and screen_w > 0 and screen_h > 0:
            x1, y1, x2, y2 = bounds
            node_w = x2 - x1
            node_h = y2 - y1
            area_ratio = (node_w * node_h) / float(screen_w * screen_h)
            width_ratio = node_w / float(screen_w)
            height_ratio = node_h / float(screen_h)
            if area_ratio >= 0.90 or (width_ratio >= 0.98 and height_ratio >= 0.98):
                continue

        rid = str(node.attrib.get("resource-id", "") or "").strip()
        has_rid = bool(rid) and rid not in {"android:id/content"}
        has_text = bool(str(node.attrib.get("text", "") or "").strip())
        has_desc = bool(str(node.attrib.get("content-desc", "") or "").strip())
        clickable = str(node.attrib.get("clickable", "false")).lower() == "true"

        if has_text or has_desc or has_rid or clickable:
            count += 1

    return count


def main() -> int:
    args = parse_args()
    runner, appium = build_runner(args)
    try:
        print("=== meaningful xml node debug ===")
        print(f"threshold={args.threshold}")
        print("每次按回车采集当前页面；输入 q 再回车结束。")

        while True:
            cmd = input("\n按回车开始采集当前页面，或输入 q 退出: ").strip().lower()
            if cmd == "q":
                break

            t0 = time.perf_counter()
            snap = runner._capture_and_process()
            elapsed = time.perf_counter() - t0
            if not snap:
                print("snap 生成失败")
                continue

            xml_processed = str(snap.get("xml") or "")
            xml_raw = str(snap.get("xml_raw") or "")
            t_xml_raw = time.perf_counter()
            raw_count = count_meaningful_xml_nodes(xml_raw)
            raw_elapsed = time.perf_counter() - t_xml_raw

            t_xml_processed = time.perf_counter()
            processed_count = count_meaningful_xml_nodes(xml_processed)
            processed_elapsed = time.perf_counter() - t_xml_processed

            print("\n=== 当前页面结果 ===")
            print(f"snap_elapsed_s={elapsed:.4f}")
            print(f"state_sig={snap.get('state_sig')}")
            print(f"xml_reliable(current)={snap.get('xml_reliable')}")
            print(f"meaningful_nodes_raw={raw_count}")
            print(f"meaningful_nodes_processed={processed_count}")
            print(f"raw_count_elapsed_s={raw_elapsed:.6f}")
            print(f"processed_count_elapsed_s={processed_elapsed:.6f}")
            print(f"raw_xml_reliable_by_threshold={raw_count >= int(args.threshold)}")
            print(f"processed_xml_reliable_by_threshold={processed_count >= int(args.threshold)}")

        return 0
    finally:
        appium.quit()


if __name__ == "__main__":
    raise SystemExit(main())
