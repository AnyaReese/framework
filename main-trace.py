"""CLI helper to inspect a recorded trace.jsonl.

Each line in trace.jsonl is a JSON object with:
  { "event": str, "ts": float, "ctx": {...}, "data": {...} }

This tool prints a human-friendly stream with timestamps, event type, and a short
result/summary. Use filters to narrow by event kind.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union


def load_events(path: str) -> Iterable[Dict]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def ts_fmt(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return f"{ts:.3f}"


def _ctx_prefix(ev: Dict, rel0: Optional[float], rel: bool, labels: Dict[str, str]) -> str:
    ctx = ev.get("ctx") or {}
    ts = ev.get("ts", 0.0)
    tstr = f"+{(ts - rel0):.2f}s" if rel0 and rel else ts_fmt(ts)
    step = ctx.get("step_id")
    cur_full = ctx.get("cur_sig") or ""
    cur = cur_full[:8]
    gaps = len(ctx.get("open_gaps") or [])
    ans = ctx.get("answered_ratio")
    ans_s = f"{ans:.2f}" if isinstance(ans, (int, float)) else "?"
    depth = len(ctx.get("stack") or [])
    label = labels.get(cur_full) or ""
    if label:
        label = label.strip()
        if len(label) > 36:
            label = label[:33] + "..."
        label_s = f" \"{label}\""
    else:
        label_s = ""
    return f"[{tstr}] step={step} sig={cur}{label_s} gaps={gaps} ans={ans_s} depth={depth}"


def _compact(obj: Union[Dict, List, str, int, float, None], limit: int = 120) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= limit else s[: limit - 3] + "..."

def _preview_text(result: Dict, keys: List[str]) -> str:
    for k in keys:
        if not k:
            continue
        parts = k.split(".")
        cur = result
        ok = True
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur.get(p)
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return ""

def _flatten_uist(uist: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(n: Dict[str, Any]) -> None:
        out.append(n)
        for ch in (n.get("subviews") or []):
            if isinstance(ch, dict):
                walk(ch)

    for r in (uist.get("elements") or []):
        if isinstance(r, dict):
            walk(r)
    return out


def _label_score(label: str, bounds: Tuple[float, float, float, float], screen_h: float) -> float:
    x, y, w, h = bounds
    if not label:
        return -1.0
    s = 0.0
    if screen_h > 0 and y < 0.35 * screen_h:
        s += 2.0
    s += min(2.5, len(label) / 20.0)
    if h > 0 and h < 0.15 * max(1.0, screen_h):
        s += 0.6
    if len(label) > 80:
        s -= 1.0
    return s


def _extract_label_from_uist(uist: Dict[str, Any]) -> str:
    nodes = _flatten_uist(uist)
    if not nodes:
        return ""
    max_h = 0.0
    for n in nodes:
        f = n.get("absolute_frame") or n.get("frame") or {}
        max_h = max(max_h, float(f.get("y", 0)) + float(f.get("height", 0)))

    best = ("", -1.0)
    for n in nodes:
        label = (
            (n.get("text") or n.get("content_desc") or n.get("ocr_text") or n.get("icon_label") or "")
        )
        label = str(label).strip()
        if not label:
            continue
        f = n.get("absolute_frame") or n.get("frame") or {}
        b = (
            float(f.get("x", 0)),
            float(f.get("y", 0)),
            float(f.get("width", 0)),
            float(f.get("height", 0)),
        )
        score = _label_score(label, b, max_h)
        if score > best[1]:
            best = (label, score)
    return best[0]


def _extract_label_from_vid_summary(vid: Dict[str, Any]) -> str:
    best = ("", -1.0)
    # estimate screen height
    screen_h = 0.0
    for _, v in (vid or {}).items():
        try:
            b = v.get("bounds") or [0, 0, 0, 0]
            screen_h = max(screen_h, float(b[1]) + float(b[3]))
        except Exception:
            continue

    for _, v in (vid or {}).items():
        try:
            label = (
                (v.get("text") or v.get("content_desc") or v.get("ocr_text") or v.get("icon_label") or "")
            )
            label = str(label).strip()
            if not label:
                continue
            b = v.get("bounds") or [0, 0, 0, 0]
            bounds = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            score = _label_score(label, bounds, screen_h)
            if score > best[1]:
                best = (label, score)
        except Exception:
            continue
    return best[0]

def summarize(ev: Dict, rel0: Optional[float], rel_time: bool, labels: Dict[str, str]) -> str:
    kind = ev.get("event")
    data = ev.get("data") or {}
    ctx = ev.get("ctx") or {}
    cur = (ctx.get("cur_sig") or "")[:8]
    prefix = _ctx_prefix(ev, rel0, rel_time, labels)

    if kind == "snapshot":
        sig = data.get("state_sig", "")[:8]
        return f"{prefix} snapshot sig={sig or cur} xml={bool(data.get('xml_path'))} png={bool(data.get('screenshot_path'))} uist={bool(data.get('uist_path'))}"

    if kind == "llm_enqueued":
        return f"{prefix} llm_enqueued kind={data.get('kind')} sig={(data.get('state_sig') or cur)[:8]} open_gaps={len(data.get('open_gaps') or [])}"

    if kind == "llm_result":
        llm_kind = data.get("kind")
        dur = data.get("duration_s")
        dur_s = f"{dur:.2f}s" if isinstance(dur, (int, float)) else "?"
        if data.get("error"):
            return f"{prefix} llm_result kind={llm_kind} sig={(data.get('state_sig') or cur)[:8]} ERROR={data.get('error')} dur={dur_s}"

        result = data.get("result") or {}
        if llm_kind == "nav":
            overlay = result.get("overlay_kind")
            cands = len(result.get("candidate_actions") or [])
            page = _preview_text(result, ["page_summary", "ui_view.description"])
            if page and len(page) > 70:
                page = page[:67] + "..."
            page_s = f"\"{page}\"" if page else "\"(no summary)\""
            return f"{prefix} llm_result NAV overlay={overlay} cands={cands} page={page_s} dur={dur_s}"
        if llm_kind == "questionnaire":
            pu = len(result.get("proposed_updates") or [])
            sigs = len(result.get("detected_signals") or [])
            conf = len(result.get("conflicts") or [])
            first_upd = (result.get("proposed_updates") or [{}])[:1][0] or {}
            qid = first_upd.get("question_id")
            ans = first_upd.get("new_answer")
            preview = f"{qid}:{ans}" if qid else ""
            preview_s = _compact(preview) if preview else ""
            return f"{prefix} llm_result Q updates={pu} signals={sigs} conflicts={conf} {preview_s} dur={dur_s}"
        if llm_kind == "recovery":
            steps = len(result.get("candidate_actions") or [])
            why = _preview_text(result, ["why", "page_summary", "ui_view.description"])
            if why and len(why) > 60:
                why = why[:57] + "..."
            overlay = result.get("overlay_kind")
            why_s = f"\"{why}\"" if why else "\"(no summary)\""
            return f"{prefix} llm_result REC steps={steps} overlay={overlay} why={why_s} dur={dur_s}"
        return f"{prefix} llm_result kind={llm_kind} dur={dur_s} result={_compact(result)}"

    if kind == "action":
        action = data.get("action") or {}
        phase = data.get("phase")
        succ = data.get("extra", {}).get("success")
        reason = data.get("extra", {}).get("reason")
        has_el = data.get("extra", {}).get("has_element")
        return f"{prefix} action phase={phase} a={action.get('action')} eid={action.get('element_id')} text={action.get('text')} ok={succ} reason={reason} has_el={has_el}"

    if kind == "transition":
        if data.get("kind") == "observation":
            sig = data.get("sig", "")[:8]
            label = labels.get(data.get("sig", ""), "")
            lab_s = f" {label[:30]}" if label else ""
            return f"{prefix} observation sig={sig}{lab_s}"
        src = data.get("src", "")
        dst = data.get("dst", "")
        src_label = labels.get(src, "")
        dst_label = labels.get(dst, "")
        if src_label and len(src_label) > 26:
            src_label = src_label[:23] + "..."
        if dst_label and len(dst_label) > 26:
            dst_label = dst_label[:23] + "..."
        src_s = src[:8] + (f" \"{src_label}\"" if src_label else "")
        dst_s = dst[:8] + (f" \"{dst_label}\"" if dst_label else "")
        return f"{prefix} transition {src_s} -> {dst_s} new={data.get('dst_was_new')}"

    if kind == "questionnaire_update":
        n = len(data.get("proposed_updates") or [])
        sig = (data.get("state_sig") or cur)[:8]
        return f"{prefix} questionnaire_update sig={sig} proposed={n} signals={len(data.get('detected_signals') or [])}"

    if kind == "decision":
        detail = {k: v for k, v in data.items() if k != "name"}
        return f"{prefix} decision {data.get('name')} detail={_compact(detail)}"

    return f"{prefix} {kind} { _compact(data) }"


def update_labels_from_llm(ev: Dict, labels: Dict[str, str]) -> None:
    """Keep a running map of sig -> short description (page summary)."""
    kind = ev.get("event")
    data = ev.get("data") or {}
    ctx = ev.get("ctx") or {}
    sig = (data.get("state_sig") or ctx.get("cur_sig") or "") or ""

    if kind == "llm_result":
        result = data.get("result") or {}
        if data.get("kind") == "nav":
            page = _preview_text(result, ["page_summary", "ui_view.description"])
            if sig and page:
                labels[sig] = page
        if data.get("kind") == "recovery":
            page = _preview_text(result, ["page_summary", "ui_view.description"])
            if sig and page:
                labels[sig] = page


def update_labels_from_snapshot(ev: Dict, labels: Dict[str, str]) -> None:
    kind = ev.get("event")
    data = ev.get("data") or {}
    ctx = ev.get("ctx") or {}
    sig = (data.get("state_sig") or ctx.get("cur_sig") or "") or ""

    if kind == "snapshot":
        if sig and sig not in labels:
            label = ""
            uist_path = data.get("uist_path")
            if uist_path and os.path.isfile(uist_path):
                try:
                    with open(uist_path, "r", encoding="utf-8") as fh:
                        uist = json.load(fh)
                    label = _extract_label_from_uist(uist)
                except Exception:
                    label = ""
            if not label:
                label = _extract_label_from_vid_summary(data.get("vid_map_summary") or {})
            if label:
                labels[sig] = label


#============================  # Dev-default args for quick local run; comment out in production.
# sys.argv = [sys.argv[0], 
#             "--run-id", "20251219_212517_com.android.settings",
#             "--relative-time"]
#============================
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="View a recorded trace.jsonl")
    p.add_argument("--trace", type=str, default=None, help="Path to trace.jsonl (overrides --root/--run-id)")
    p.add_argument("--root", type=str, default="traces", help="Root trace directory (default: traces)")
    p.add_argument("--run-id", type=str, default=None, help="Run ID folder inside --root")
    p.add_argument("--events", type=str, default=None, help="Comma-separated event kinds to show (snapshot,llm_result,action,transition,decision,questionnaire_update,llm_enqueued)")
    p.add_argument("--limit", type=int, default=None, help="Only show the last N events")
    p.add_argument("--relative-time", action="store_true", help="Show timestamps relative to first event")
    p.add_argument("--full", action="store_true", help="Dump full JSON per event")
    args = p.parse_args(argv)

    if args.trace:
        trace_path = args.trace
    else:
        if not args.run_id:
            p.error("Either --trace or --run-id must be provided.")
        trace_path = os.path.join(args.root, args.run_id, "trace.jsonl")

    if not os.path.isfile(trace_path):
        print(f"Trace file not found: {trace_path}", file=sys.stderr)
        return 1

    filt = None
    if args.events:
        filt = {s.strip() for s in args.events.split(",") if s.strip()}

    events = list(load_events(trace_path))
    if args.limit:
        events = events[-args.limit :]

    labels: Dict[str, str] = {}
    rel0 = events[0]["ts"] if (events and args.relative_time) else None

    # First pass: collect NAV/REC summaries so earlier sigs can show labels.
    for ev in events:
        update_labels_from_llm(ev, labels)

    for ev in events:
        # Snapshot fallback fills in any missing labels.
        update_labels_from_snapshot(ev, labels)
        kind = ev.get("event")
        if filt and kind not in filt:
            continue
        if args.full:
            print(json.dumps(ev, ensure_ascii=False, indent=2))
        else:
            print(summarize(ev, rel0, args.relative_time, labels))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
