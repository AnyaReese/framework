from __future__ import annotations

import json
import os
from os.path import abspath, basename, dirname, exists, join, splitext
from typing import Any

os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_enable_mkldnn", "0")
os.environ.setdefault("FLAGS_enable_onednn", "0")
os.environ.setdefault("PADDLE_DISABLE_ONEDNN", "1")

from LayoutCoder_develop.run_single import uied


def _load_json(path: str, default: Any) -> Any:
    if not exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _bbox_from_position(position: dict) -> list[int]:
    return [
        int(position["column_min"]),
        int(position["row_min"]),
        int(position["column_max"]),
        int(position["row_max"]),
    ]


def _clamp(value: float, min_value: float = 0.0, max_value: float = 1.0) -> float:
    return max(min_value, min(max_value, value))


ACTION_KEYWORDS_EN = {
    "close", "skip", "next", "continue", "submit", "confirm", "agree", "allow",
    "buy", "pay", "purchase", "install", "download", "open", "start", "login",
    "log in", "sign in", "register", "join", "search", "back", "cancel",
    "ok", "okay", "done", "more", "menu", "share", "send", "apply", "use",
    "unlock", "claim", "get", "collect",
}

ACTION_KEYWORDS_ZH = {
    "关闭", "跳过", "下一步", "继续", "提交", "确认", "同意", "允许", "购买", "支付",
    "下载", "打开", "开始", "登录", "注册", "搜索", "返回", "取消", "完成", "更多",
    "菜单", "分享", "发送", "使用", "解锁", "领取", "立即", "去", "查看", "点击",
    "领", "抢", "下单",
}


def _guess_element_type(raw_class: str, text: str, bbox: list[int], img_shape: list[int] | None) -> str:
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    lower_text = text.lower().strip()

    if any(token in lower_text for token in ["close", "skip", "x", "关闭", "跳过"]):
        return "close_button"
    if raw_class == "Text":
        if any(token in lower_text for token in ACTION_KEYWORDS_EN) or any(token in text for token in ACTION_KEYWORDS_ZH):
            return "button_like_text"
        return "text_block"
    if width <= 64 and height <= 64:
        if img_shape:
            img_h, img_w = img_shape[0], img_shape[1]
            if bbox[1] <= img_h * 0.15 and (bbox[0] <= img_w * 0.15 or bbox[2] >= img_w * 0.85):
                return "corner_icon"
        return "icon_like"
    if width > height * 1.8 and 28 <= height <= 120:
        return "button_like"
    if width >= 120 and height >= 80:
        return "image_or_card"
    return "container"


def _estimate_clickability(element_type: str, raw_class: str, text: str, bbox: list[int], img_shape: list[int] | None) -> tuple[float, list[str]]:
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    area = width * height
    reasons: list[str] = []
    score = 0.12

    if raw_class == "Text":
        score += 0.06
        reasons.append("text element")
    else:
        score += 0.12
        reasons.append("non-text visual component")

    if element_type in {"close_button", "corner_icon"}:
        score += 0.48
        reasons.append("looks like dismiss or corner action")
    elif element_type in {"button_like", "button_like_text"}:
        score += 0.35
        reasons.append("button-like shape or action text")
    elif element_type == "icon_like":
        score += 0.22
        reasons.append("small icon-sized component")
    elif element_type == "image_or_card":
        score += 0.14
        reasons.append("card-sized component may be tappable")

    compact_text = text.lower().replace(" ", "")
    if any(token in compact_text for token in ACTION_KEYWORDS_EN):
        score += 0.22
        reasons.append("contains action-oriented English text")
    if any(token in text for token in ACTION_KEYWORDS_ZH):
        score += 0.22
        reasons.append("contains action-oriented Chinese text")

    if width < 20 or height < 20:
        score -= 0.18
        reasons.append("very small target")
    elif width >= 32 and height >= 32:
        score += 0.06
        reasons.append("reasonable tap target size")

    if img_shape:
        img_area = img_shape[0] * img_shape[1]
        area_ratio = area / img_area
        if area_ratio > 0.35:
            score -= 0.25
            reasons.append("too large, likely container/background")
        elif area_ratio < 0.0004:
            score -= 0.08
            reasons.append("tiny visual region")

    return _clamp(score), reasons


def run_uied_tool(image_path: str, output_root: str | None = None) -> dict:
    image_path = abspath(image_path)
    if output_root is None:
        output_root = join(dirname(__file__), "outputs", splitext(basename(image_path))[0])
    os.makedirs(output_root, exist_ok=True)

    uied(input_path_img=image_path, output_root=output_root)

    name = splitext(basename(image_path))[0]
    file_map = {
        "ocr": join(output_root, "ocr", f"{name}.json"),
        "ip": join(output_root, "ip", f"{name}.json"),
        "merge": join(output_root, "merge", f"{name}.json"),
        "layout": join(output_root, "layout", f"{name}.json"),
    }

    if not exists(file_map["merge"]):
        raise FileNotFoundError(f"UIED merge output not found: {file_map['merge']}")

    return {
        "image_path": image_path,
        "output_root": abspath(output_root),
        "ocr": _load_json(file_map["ocr"], {"img_shape": None, "texts": []}),
        "ip": _load_json(file_map["ip"], {"img_shape": None, "compos": []}),
        "merge": _load_json(file_map["merge"], {"img_shape": None, "compos": []}),
        "layout": _load_json(file_map["layout"], []),
    }


def build_llm_page_elements(uied_result: dict, output_path: str | None = None) -> dict:
    merge_data = uied_result.get("merge", {})
    ocr_data = uied_result.get("ocr", {})
    img_shape = merge_data.get("img_shape") or ocr_data.get("img_shape")

    page_elements = []
    clickable_elements = []

    for compo in merge_data.get("compos", []):
        bbox = _bbox_from_position(compo["position"])
        text = (compo.get("text_content") or "").strip()
        raw_class = compo.get("class", "Unknown")
        element_type = _guess_element_type(raw_class, text, bbox, img_shape)
        clickable_score, clickable_reasons = _estimate_clickability(element_type, raw_class, text, bbox, img_shape)

        element = {
            "id": compo.get("id"),
            "bbox": bbox,
            "width": int(compo.get("width", bbox[2] - bbox[0])),
            "height": int(compo.get("height", bbox[3] - bbox[1])),
            "center": [int((bbox[0] + bbox[2]) / 2), int((bbox[1] + bbox[3]) / 2)],
            "raw_class": raw_class,
            "element_type": element_type,
            "text": text,
            "clickable_score": round(clickable_score, 3),
            "clickable": clickable_score >= 0.5,
            "clickable_reasons": clickable_reasons,
            "source": "uied_merge",
        }
        page_elements.append(element)

        if element["clickable"]:
            clickable_elements.append({
                "id": element["id"],
                "bbox": element["bbox"],
                "center": element["center"],
                "element_type": element["element_type"],
                "text": element["text"],
                "clickable_score": element["clickable_score"],
                "clickable_reasons": element["clickable_reasons"],
            })

    page_texts = []
    for text_item in ocr_data.get("texts", []):
        page_texts.append({
            "id": text_item.get("id"),
            "text": text_item.get("content", ""),
            "bbox": [
                int(text_item["column_min"]),
                int(text_item["row_min"]),
                int(text_item["column_max"]),
                int(text_item["row_max"]),
            ],
        })

    normalized = {
        "image_path": uied_result.get("image_path"),
        "image_shape": img_shape,
        "page_text": " ".join(item["text"] for item in page_texts if item["text"]).strip(),
        "page_texts": page_texts,
        "page_elements": page_elements,
        "clickable_elements": clickable_elements,
        "layout_blocks": uied_result.get("layout", []),
        "raw_output_root": uied_result.get("output_root"),
    }

    if output_path:
        output_path = abspath(output_path)
        os.makedirs(dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)

    return normalized
