"""Lightweight callback interfaces and a JSONL trace writer.

This module defines a small callback protocol that the workflow can invoke at
authoritative choke points (snapshots, LLM I/O, actions, transitions,
questionnaire updates, and policy decisions). A no-op implementation is the
default; JsonlTraceCallbacks writes compact events and large artifacts (XML/
screenshot/UI trees) to disk for offline replay and analysis.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from PIL import Image, ImageDraw


@dataclass
class StepCtx:
    run_id: str
    step_id: int
    ts: float
    cur_sig: str
    stack: List[str]
    block_status: Dict[str, Any]
    open_gaps: List[str]
    answered_ratio: float


class Callbacks(Protocol):
    def on_snapshot(self, ctx: StepCtx, snap: Dict[str, Any]) -> None: ...

    def on_llm_enqueued(self, ctx: StepCtx, kind: str, payload: Dict[str, Any]) -> None: ...

    def on_llm_result(self, ctx: StepCtx, kind: str, result: Dict[str, Any]) -> None: ...

    def on_action(self, ctx: StepCtx, action: Dict[str, Any], phase: str, extra: Dict[str, Any]) -> None: ...

    def on_transition(self, ctx: StepCtx, tr: Dict[str, Any]) -> None: ...

    def on_questionnaire_update(self, ctx: StepCtx, upd: Dict[str, Any]) -> None: ...

    def on_decision(self, ctx: StepCtx, name: str, detail: Dict[str, Any]) -> None: ...


class NoOpCallbacks:
    def on_snapshot(self, ctx: StepCtx, snap: Dict[str, Any]) -> None:
        return None

    def on_llm_enqueued(self, ctx: StepCtx, kind: str, payload: Dict[str, Any]) -> None:
        return None

    def on_llm_result(self, ctx: StepCtx, kind: str, result: Dict[str, Any]) -> None:
        return None

    def on_action(self, ctx: StepCtx, action: Dict[str, Any], phase: str, extra: Dict[str, Any]) -> None:
        return None

    def on_transition(self, ctx: StepCtx, tr: Dict[str, Any]) -> None:
        return None

    def on_questionnaire_update(self, ctx: StepCtx, upd: Dict[str, Any]) -> None:
        return None

    def on_decision(self, ctx: StepCtx, name: str, detail: Dict[str, Any]) -> None:
        return None


class InteractiveDebugCallbacks(NoOpCallbacks):
    """Print concise Chinese step summaries and wait for a key between steps."""

    manages_action_pause = True

    def __init__(self, *, pause_on: Optional[List[str]] = None) -> None:
        self.pause_on = set(
            pause_on
            or [
                "snapshot",
                "llm_result",
                "action_before",
                "action_after",
                "transition",
                "questionnaire_update",
                "decision",
            ]
        )
        self._seq = 0
        self._lock = threading.Lock()

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _short_sig(self, sig: Any) -> str:
        text = str(sig or "")
        return text[:8] if text else "-"

    def _short_text(self, value: Any, limit: int = 80) -> str:
        text = str(value or "").strip().replace("\n", " ")
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _fmt_action(self, action: Dict[str, Any]) -> str:
        kind = str(action.get("action") or "?")
        element_id = action.get("element_id")
        text = self._short_text(action.get("text"), 30)
        parts = [kind]
        if element_id is not None:
            parts.append(f"id={element_id}")
        if text:
            parts.append(f"text={text}")
        return " ".join(parts)

    def _fmt_json(self, payload: Dict[str, Any], limit: int = 160) -> str:
        return self._short_text(json.dumps(payload, ensure_ascii=False), limit)

    def _print_block(self, title: str, lines: List[str], *, pause_key: Optional[str] = None) -> None:
        idx = self._next_seq()
        print(f"\n[{idx:03d}] {title}")
        for line in lines:
            print(f"  {line}")
        if pause_key and pause_key in self.pause_on:
            self._wait_for_key()

    def _wait_for_key(self) -> None:
        prompt = "  按任意键继续..."
        if os.name == "nt":
            try:
                import msvcrt

                print(prompt, end="", flush=True)
                msvcrt.getwch()
                print("")
                return
            except Exception:
                pass
        input(f"{prompt}（或直接回车）")

    def on_snapshot(self, ctx: StepCtx, snap: Dict[str, Any]) -> None:  # type: ignore[override]
        meta = snap.get("meta") or {}
        vid_count = len(snap.get("vid_map") or {})
        root_count = len((snap.get("uist") or {}).get("elements") or [])
        self._print_block(
            "快照",
            [
                f"step_id={ctx.step_id} sig={self._short_sig(snap.get('state_sig'))} stack_depth={len(ctx.stack)}",
                f"前台包={meta.get('foreground_package') or '-'} activity={meta.get('foreground_activity') or '-'}",
                f"元素数={vid_count} 根节点数={root_count} cache_hit={bool(meta.get('cache_hit'))}",
            ],
            pause_key="snapshot",
        )

    def on_llm_result(self, ctx: StepCtx, kind: str, result: Dict[str, Any]) -> None:  # type: ignore[override]
        keys = sorted(result.keys())
        self._print_block(
            f"LLM结果: {kind}",
            [
                f"sig={self._short_sig(ctx.cur_sig)} 字段={', '.join(keys[:8]) or '-'}",
                f"摘要={self._short_text(json.dumps(result, ensure_ascii=False), 160)}",
            ],
            pause_key="llm_result",
        )

    def on_action(self, ctx: StepCtx, action: Dict[str, Any], phase: str, extra: Dict[str, Any]) -> None:  # type: ignore[override]
        origin = str(extra.get("origin") or "-")
        reasoning = self._short_text(extra.get("reasoning"), 100) or "-"
        if phase == "before":
            self._print_block(
                "动作准备",
                [
                    f"sig={self._short_sig(ctx.cur_sig)} 动作={self._fmt_action(action)}",
                    f"来源函数={origin}",
                    f"执行原因={reasoning}",
                    f"附加信息={self._fmt_json(extra, 140)}",
                ],
                pause_key="action_before",
            )
            return

        self._print_block(
            "动作结果",
            [
                f"sig={self._short_sig(ctx.cur_sig)} 动作={self._fmt_action(action)}",
                f"来源函数={origin}",
                f"执行原因={reasoning}",
                f"结果={self._fmt_json(extra, 140)}",
            ],
            pause_key="action_after",
        )

    def on_transition(self, ctx: StepCtx, tr: Dict[str, Any]) -> None:  # type: ignore[override]
        self._print_block(
            "状态迁移",
            [
                f"kind={tr.get('kind') or '-'} src={self._short_sig(tr.get('src'))} dst={self._short_sig(tr.get('dst') or tr.get('sig'))}",
                f"详情={self._short_text(json.dumps(tr, ensure_ascii=False), 160)}",
            ],
            pause_key="transition",
        )

    def on_questionnaire_update(self, ctx: StepCtx, upd: Dict[str, Any]) -> None:  # type: ignore[override]
        self._print_block(
            "问卷更新",
            [
                f"sig={self._short_sig(ctx.cur_sig)}",
                f"详情={self._short_text(json.dumps(upd, ensure_ascii=False), 160)}",
            ],
            pause_key="questionnaire_update",
        )

    def on_decision(self, ctx: StepCtx, name: str, detail: Dict[str, Any]) -> None:  # type: ignore[override]
        if name == "next_step":
            plan = str(detail.get("plan") or "-")
            self._print_block(
                "下一步计划",
                [
                    f"当前sig={self._short_sig(ctx.cur_sig)}",
                    f"计划={plan}",
                    f"详情={self._short_text(json.dumps(detail, ensure_ascii=False), 220)}",
                ],
                pause_key="decision",
            )
            return
        self._print_block(
            f"流程决策: {name}",
            [
                f"sig={self._short_sig(ctx.cur_sig)} blocks={len(ctx.block_status)}",
                f"详情={self._short_text(json.dumps(detail, ensure_ascii=False), 220)}",
            ],
            pause_key="decision",
        )


class JsonlTraceCallbacks(NoOpCallbacks):
    """Persist events + large blobs to disk for offline replay/metrics."""

    def __init__(self, root_dir: str, run_id: Optional[str] = None) -> None:
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S")
        self.root_dir = os.path.abspath(os.path.join(root_dir, self.run_id))
        self.trace_path = os.path.join(self.root_dir, "trace.jsonl")

        os.makedirs(os.path.join(self.root_dir, "screens"), exist_ok=True)
        os.makedirs(os.path.join(self.root_dir, "screens_raw"), exist_ok=True)
        os.makedirs(os.path.join(self.root_dir, "xml"), exist_ok=True)
        os.makedirs(os.path.join(self.root_dir, "xml_raw"), exist_ok=True)
        os.makedirs(os.path.join(self.root_dir, "uist"), exist_ok=True)
        os.makedirs(os.path.join(self.root_dir, "screens_annotated"), exist_ok=True)
        os.makedirs(os.path.join(self.root_dir, "screens_annotated", "vid_map"), exist_ok=True)
        os.makedirs(os.path.join(self.root_dir, "screens_annotated", "uist"), exist_ok=True)

        self._lock = threading.Lock()

    # ------------ helpers ------------
    def _write_event(self, event: str, ctx: StepCtx, payload: Dict[str, Any]) -> None:
        rec = {
            "event": event,
            "ts": time.time(),
            "ctx": {
                "run_id": ctx.run_id,
                "step_id": ctx.step_id,
                "ts": ctx.ts,
                "cur_sig": ctx.cur_sig,
                "stack": ctx.stack,
                "block_status": ctx.block_status,
                "open_gaps": ctx.open_gaps,
                "answered_ratio": ctx.answered_ratio,
            },
            "data": payload,
        }
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            with open(self.trace_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    @staticmethod
    def _safe_filename_token(value: Any, default: str = "state") -> str:
        """
        Convert state ids into Windows-safe filename tokens.

        Why:
        - New state_sig values may contain ":" (for example "xml:abcd...").
        - On Windows, ":" creates an alternate data stream, causing normal
          files to appear as 0-byte placeholders.
        """
        token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("_")
        return token or default

    @staticmethod
    def _vid_map_summary(vid_map: Dict[Any, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in (vid_map or {}).items():
            try:
                f = v.get("absolute_frame") or v.get("frame") or {}
                out[str(k)] = {
                    "bounds": [
                        int(f.get("x", 0)),
                        int(f.get("y", 0)),
                        int(f.get("width", 0)),
                        int(f.get("height", 0)),
                    ],
                    "text": v.get("text"),
                    "content_desc": v.get("content_desc"),
                    "ocr_text": v.get("ocr_text"),
                    "class": v.get("class"),
                    "clickable": v.get("clickable"),
                    "enabled": v.get("enabled"),
                }
            except Exception:
                continue
        return out

    @staticmethod
    def _iter_uist_nodes(uist: Dict[str, Any]):
        stack = list(uist.get("elements", []) or [])
        while stack:
            node = stack.pop()
            yield node
            subs = node.get("subviews", []) or []
            if subs:
                stack.extend(reversed(subs))

    @staticmethod
    def _decode_screenshot(screenshot_b64: str) -> Optional[Image.Image]:
        if not screenshot_b64:
            return None
        try:
            raw = base64.b64decode(screenshot_b64)
            return Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            return None

    @staticmethod
    def _label_for_node(node: Dict[str, Any]) -> str:
        for key in ("text", "content_desc", "ocr_text", "semantic_label", "icon_label"):
            value = node.get(key)
            if value:
                return str(value).strip()
        return ""

    @staticmethod
    def _draw_boxes(
        image: Image.Image,
        items: List[Dict[str, Any]],
        *,
        id_getter,
        color_getter,
    ) -> Image.Image:
        out = image.copy()
        draw = ImageDraw.Draw(out)

        for item in items:
            frame = item.get("absolute_frame") or item.get("frame") or {}
            x = int(frame.get("x", 0))
            y = int(frame.get("y", 0))
            w = int(frame.get("width", 0))
            h = int(frame.get("height", 0))
            if w <= 0 or h <= 0:
                continue

            color = color_getter(item)
            draw.rectangle((x, y, x + w, y + h), outline=color, width=3)

            label_text = str(id_getter(item))
            if not label_text:
                continue
            label_box = draw.textbbox((x, y), label_text)
            text_w = label_box[2] - label_box[0]
            text_h = label_box[3] - label_box[1]
            text_left = max(0, x)
            text_top = max(0, y - text_h - 8)
            if text_top == 0 and y + h + text_h + 8 < out.height:
                text_top = y + h + 2
            draw.rectangle(
                (text_left, text_top, text_left + text_w + 10, text_top + text_h + 8),
                fill=color,
            )
            draw.text((text_left + 5, text_top + 4), label_text, fill=(255, 255, 255))

        return out

    def _write_snapshot_overlays(self, prefix: str, snap: Dict[str, Any]) -> Dict[str, Optional[str]]:
        screenshot_b64 = str(snap.get("screenshot") or "")
        base_img = self._decode_screenshot(screenshot_b64)
        if base_img is None:
            return {"vidmap_overlay_path": None, "uist_overlay_path": None}

        annotated_dir = os.path.join(self.root_dir, "screens_annotated")
        vidmap_dir = os.path.join(annotated_dir, "vid_map")
        uist_dir = os.path.join(annotated_dir, "uist")
        vidmap_path = None
        uist_path = None

        try:
            vid_items = list((snap.get("vid_map") or {}).values())
            vid_img = self._draw_boxes(
                base_img,
                vid_items,
                id_getter=lambda n: n.get("id", ""),
                color_getter=lambda n: (220, 50, 47) if bool(n.get("clickable")) else (46, 160, 67),
            )
            vidmap_path = os.path.join(vidmap_dir, f"{prefix}.png")
            vid_img.save(vidmap_path)
        except Exception:
            vidmap_path = None

        try:
            uist_items = list(self._iter_uist_nodes(snap.get("uist") or {}))
            # uist 视图没有稳定 id 时，优先显示节点 id；没有 id 时显示流水号会不稳定，
            # 所以这里退化成只显示已有 id，没有就不显示文字标签。
            uist_img = self._draw_boxes(
                base_img,
                uist_items,
                id_getter=lambda n: n.get("id", ""),
                color_getter=lambda n: (203, 75, 22) if bool(n.get("clickable")) else (133, 153, 0),
            )
            uist_path = os.path.join(uist_dir, f"{prefix}.png")
            uist_img.save(uist_path)
        except Exception:
            uist_path = None

        return {"vidmap_overlay_path": vidmap_path, "uist_overlay_path": uist_path}

    # ------------ event handlers ------------
    def on_snapshot(self, ctx: StepCtx, snap: Dict[str, Any]) -> None:  # type: ignore[override]
        sig = str(snap.get("state_sig") or "")
        prefix = f"{ctx.step_id:06d}_{self._safe_filename_token(sig)[:48]}"

        screenshot_path = None
        screenshot_hash = None
        if snap.get("screenshot"):
            try:
                raw = base64.b64decode(snap.get("screenshot") or b"")
                screenshot_hash = hashlib.md5(raw).hexdigest()
                screenshot_path = os.path.join(self.root_dir, "screens", f"{prefix}.png")
                with open(screenshot_path, "wb") as fh:
                    fh.write(raw)
            except Exception:
                screenshot_path = None

        screenshot_raw_path = None
        screenshot_raw_hash = None
        if snap.get("screenshot_raw"):
            try:
                raw2 = base64.b64decode(snap.get("screenshot_raw") or b"")
                screenshot_raw_hash = hashlib.md5(raw2).hexdigest()
                screenshot_raw_path = os.path.join(self.root_dir, "screens_raw", f"{prefix}.png")
                with open(screenshot_raw_path, "wb") as fh:
                    fh.write(raw2)
            except Exception:
                screenshot_raw_path = None

        xml_path = None
        if snap.get("xml"):
            try:
                xml_path = os.path.join(self.root_dir, "xml", f"{prefix}.xml")
                with open(xml_path, "w", encoding="utf-8") as fh:
                    fh.write(str(snap.get("xml") or ""))
            except Exception:
                xml_path = None

        xml_raw_path = None
        if snap.get("xml_raw"):
            try:
                xml_raw_path = os.path.join(self.root_dir, "xml_raw", f"{prefix}.xml")
                with open(xml_raw_path, "w", encoding="utf-8") as fh:
                    fh.write(str(snap.get("xml_raw") or ""))
            except Exception:
                xml_raw_path = None

        uist_path = None
        if snap.get("uist"):
            try:
                uist_path = os.path.join(self.root_dir, "uist", f"{prefix}.json")
                with open(uist_path, "w", encoding="utf-8") as fh:
                    json.dump(snap.get("uist") or {}, fh, ensure_ascii=False)
            except Exception:
                uist_path = None

        overlay_paths = self._write_snapshot_overlays(prefix, snap)

        payload = {
            "state_sig": sig,
            "xml_path": xml_path,
            "xml_raw_path": xml_raw_path,
            "screenshot_path": screenshot_path,
            "screenshot_hash": screenshot_hash,
            "screenshot_raw_path": screenshot_raw_path,
            "screenshot_raw_hash": screenshot_raw_hash,
            "uist_path": uist_path,
            **overlay_paths,
            "vid_map_summary": self._vid_map_summary(snap.get("vid_map", {})),
            "device_info": snap.get("device_info"),
            "meta": snap.get("meta", {}),
        }

        self._write_event("snapshot", ctx, payload)

    def on_llm_enqueued(self, ctx: StepCtx, kind: str, payload: Dict[str, Any]) -> None:  # type: ignore[override]
        self._write_event("llm_enqueued", ctx, {"kind": kind, **payload})

    def on_llm_result(self, ctx: StepCtx, kind: str, result: Dict[str, Any]) -> None:  # type: ignore[override]
        self._write_event("llm_result", ctx, {"kind": kind, **result})

    def on_action(self, ctx: StepCtx, action: Dict[str, Any], phase: str, extra: Dict[str, Any]) -> None:  # type: ignore[override]
        self._write_event("action", ctx, {"phase": phase, "action": action, "extra": extra})

    def on_transition(self, ctx: StepCtx, tr: Dict[str, Any]) -> None:  # type: ignore[override]
        self._write_event("transition", ctx, tr)

    def on_questionnaire_update(self, ctx: StepCtx, upd: Dict[str, Any]) -> None:  # type: ignore[override]
        self._write_event("questionnaire_update", ctx, upd)

    def on_decision(self, ctx: StepCtx, name: str, detail: Dict[str, Any]) -> None:  # type: ignore[override]
        self._write_event("decision", ctx, {"name": name, **detail})


__all__ = [
    "Callbacks",
    "StepCtx",
    "NoOpCallbacks",
    "JsonlTraceCallbacks",
]
