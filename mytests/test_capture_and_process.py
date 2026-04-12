from __future__ import annotations

import argparse
import base64
import json
from io import BytesIO
from pathlib import Path
import sys
from typing import Any, Dict

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 把 framework 根目录加入 sys.path。
# 这样即使我们从 mytests 目录直接执行脚本，也能正常导入：
# - appium_android.py
# - workflow.py
# - questionnaire_state.py
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from appium_android import AndroidAppiumClient
from gpt_cls import _compact_digest
from questionnaire_state import QuestionnaireState
from trace_callbacks import NoOpCallbacks
from workflow import BudgetConfig, WorkflowRunner


def _summarize_vid_map(vid_map: Dict[Any, Any], limit: int = 12) -> list[dict]:
    out = []
    for idx, (k, v) in enumerate((vid_map or {}).items()):
        if idx >= limit:
            break
        f = v.get("absolute_frame") or {}
        out.append(
            {
                "id": k,
                "class": v.get("class"),
                "text": v.get("text"),
                "content_desc": v.get("content_desc"),
                "ocr_text": v.get("ocr_text"),
                "clickable": bool(v.get("clickable")),
                "bounds": [
                    int(f.get("x", 0)),
                    int(f.get("y", 0)),
                    int(f.get("width", 0)),
                    int(f.get("height", 0)),
                ],
            }
        )
    return out


def _label_for_element(node: Dict[str, Any]) -> str:
    for key in ("text", "content_desc", "ocr_text", "semantic_label", "icon_label"):
        value = node.get(key)
        if value:
            return str(value).strip()
    return ""


def _print_readable_elements(vid_map: Dict[Any, Any], limit: int = 20) -> None:
    # 把 vid_map 中的元素打印成更适合人工查看的形式。
    # 这里的重点是：终端里快速看“识别出了哪些可操作元素”。
    print("\n=== Readable Elements ===")
    if not vid_map:
        print("(empty)")
        return
    for idx, (element_id, node) in enumerate(vid_map.items()):
        if idx >= limit:
            print(f"... truncated, total={len(vid_map)}")
            break
        frame = node.get("absolute_frame") or {}
        label = _label_for_element(node)
        class_name = node.get("class") or "Unknown"
        clickable = "Y" if node.get("clickable") else "N"
        bounds = (
            int(frame.get("x", 0)),
            int(frame.get("y", 0)),
            int(frame.get("width", 0)),
            int(frame.get("height", 0)),
        )
        print(
            f"[{element_id:>3}] click={clickable} class={class_name} "
            f"bounds={bounds} label={label or '<empty>'}"
        )


def _decode_screenshot_to_image(screenshot_b64: str) -> Image.Image:
    raw = base64.b64decode(screenshot_b64)
    return Image.open(BytesIO(raw)).convert("RGB")


def _draw_vid_map_overlay(
    screenshot_b64: str,
    vid_map: Dict[Any, Any],
    out_path: Path,
    limit: int | None = None,
) -> None:
    # 直接在截图上画框，方便人工肉眼检查。
    # 当前颜色规则：
    # - 红框：可点击
    # - 绿框：不可点击
    # 并且在框边标注元素 id，方便和终端输出对照。
    image = _decode_screenshot_to_image(screenshot_b64)
    draw = ImageDraw.Draw(image)

    for idx, (element_id, node) in enumerate(vid_map.items()):
        if limit is not None and idx >= limit:
            break
        frame = node.get("absolute_frame") or {}
        x = int(frame.get("x", 0))
        y = int(frame.get("y", 0))
        w = int(frame.get("width", 0))
        h = int(frame.get("height", 0))
        if w <= 0 or h <= 0:
            continue

        is_clickable = bool(node.get("clickable"))
        color = (220, 50, 47) if is_clickable else (46, 160, 67)
        draw.rectangle((x, y, x + w, y + h), outline=color, width=3)

        label_text = str(element_id)
        label_box = draw.textbbox((x, y), label_text)
        text_w = label_box[2] - label_box[0]
        text_h = label_box[3] - label_box[1]
        text_left = max(0, x)
        text_top = max(0, y - text_h - 8)
        if text_top == 0 and y + h + text_h + 8 < image.height:
            text_top = y + h + 2
        draw.rectangle(
            (text_left, text_top, text_left + text_w + 10, text_top + text_h + 8),
            fill=color,
        )
        draw.text((text_left + 5, text_top + 4), label_text, fill=(255, 255, 255))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run WorkflowRunner._capture_and_process() once.")
    p.add_argument("--appium-url", default="http://127.0.0.1:4723")
    p.add_argument("--device-name", default=None)
    p.add_argument("--package", required=True)
    p.add_argument("--activity", default=None)
    p.add_argument("--questionnaire-dir", default="./questionnaire-v2/games")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--out", default=None, help="Optional JSON output path")
    p.add_argument(
        "--overlay-out",
        default="./mytests/outputs/capture_overlay.png",
        help="Where to save the screenshot annotated with element boxes",
    )
    p.add_argument(
        "--overlay-limit",
        type=int,
        default=None,
        help="Only draw the first N vid_map elements if set",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    questionnaires = QuestionnaireState.load_from_dir(args.questionnaire_dir)
    appium = AndroidAppiumClient(
        server_url=args.appium_url,
        device_name=args.device_name,
    ).init_connection()

    runner = WorkflowRunner(
        appium=appium,
        gpt=None,  # _capture_and_process does not use GPT
        questionnaires=questionnaires,
        budget=BudgetConfig(),
        target_package=args.package,
        target_activity=args.activity,
        callbacks=NoOpCallbacks(),
        pause=False,
    )

    try:
        if args.package:
            appium.ensure_foreground(args.package, args.activity)

        # 这里调用一次主流程中的 capture + process，拿到：
        # - screenshot
        # - uist
        # - vid_map
        # - state_sig
        #
        # 你后续如果想切实验链，只需要把这里改成 _capture_and_process2() 即可。
        snap = runner._capture_and_process(timeout=float(args.timeout))
        if not snap:
            print("capture failed: returned None")
            return 1

        meta = snap.get("meta") or {}
        summary = {
            "state_sig": snap.get("state_sig"),
            "foreground_package": meta.get("foreground_package"),
            "foreground_activity": meta.get("foreground_activity"),
            "xml_reliable": meta.get("xml_reliable"),
            "coord_scale": meta.get("coord_scale"),
            "cache_hit": meta.get("cache_hit"),
            "uist_root_count": len((snap.get("uist") or {}).get("elements") or []),
            "vid_count": len(snap.get("vid_map") or {}),
            "ui_digest_count": len((_compact_digest(snap.get("uist") or {}, limit=240).get("elements") or [])),
            "sample_elements": _summarize_vid_map(snap.get("vid_map") or {}, limit=12),
        }

        print(json.dumps(summary, ensure_ascii=False, indent=2))
        _print_readable_elements(snap.get("vid_map") or {}, limit=20)

        overlay_out = Path(args.overlay_out).resolve()
        # 注意：这里画的是 vid_map，不是整棵 uist。
        # 所以如果某个节点已经进入 uist，但还没有被分配 id，
        # 那么它不会出现在这张图里。
        _draw_vid_map_overlay(
            screenshot_b64=snap.get("screenshot") or "",
            vid_map=snap.get("vid_map") or {},
            out_path=overlay_out,
            limit=args.overlay_limit,
        )
        print(f"\nsaved overlay image to: {overlay_out}")

        if args.out:
            out_path = Path(args.out).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # 在调试输出文件里直接附带 ui_digest，方便你对照：
            # - 原始/补充后的 uist
            # - vid_map
            # - 真正会喂给 LLM 的压缩 UI 结构
            output_payload = dict(snap)
            output_payload["ui_digest"] = _compact_digest(snap.get("uist") or {}, limit=240)
            out_path.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"saved full snapshot to: {out_path}")

        return 0
    finally:
        appium.quit()


if __name__ == "__main__":
    raise SystemExit(main())
