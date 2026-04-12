from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception as exc:
            rows.append(
                {
                    "event": "parse_error",
                    "ctx": {"step_id": None, "ts": None},
                    "data": {"line_no": line_no, "error": repr(exc), "raw": line[:200]},
                }
            )
            continue
        rows.append(obj)
    return rows


def _short_sig(sig: Any) -> str:
    s = str(sig or "")
    return s[:8] if s else "-"


def _fmt_action(action: Dict[str, Any] | None) -> str:
    if not action:
        return "-"
    return f"{action.get('action') or '-'} id={action.get('element_id')} text={action.get('text')!r}"


def _candidate_summary(result: Dict[str, Any]) -> str:
    cands = (result or {}).get("candidate_actions") or []
    parts: List[str] = []
    for cand in cands[:4]:
        actions = cand.get("actions") or []
        if not actions:
            continue
        a0 = actions[0]
        parts.append(
            f"{a0.get('action')}#{a0.get('element_id')} score={cand.get('score')}"
        )
    return "; ".join(parts) if parts else "-"


def _format_event(row: Dict[str, Any]) -> Tuple[str, str]:
    event = row.get("event") or "-"
    ctx = row.get("ctx") or {}
    data = row.get("data") or {}

    title = event
    detail = "-"

    if event == "snapshot":
        title = "抓取页面"
        meta = data.get("meta") or {}
        vid_summary = data.get("vid_map_summary") or {}
        detail = (
            f"state={_short_sig(data.get('state_sig'))} "
            f"pkg={meta.get('foreground_package') or '-'} "
            f"activity={meta.get('foreground_activity') or '-'} "
            f"vid={len(vid_summary)} "
            f"xml_reliable={meta.get('xml_reliable')}"
        )
    elif event == "transition":
        kind = data.get("kind")
        if kind == "transition":
            title = "状态跳转"
            action = ((data.get("action") or {}).get("actions") or [{}])[0]
            outcome = ((data.get("action") or {}).get("outcome") or {})
            detail = (
                f"{_short_sig(data.get('src'))} -> {_short_sig(data.get('dst'))} "
                f"via {_fmt_action(action)} "
                f"changed={outcome.get('changed')} type={outcome.get('change_type')}"
            )
        elif kind == "observation":
            title = "状态观察"
            detail = f"sig={_short_sig(data.get('sig'))} meta={data.get('meta') or {}}"
        else:
            title = "状态事件"
            detail = json.dumps(data, ensure_ascii=False)[:220]
    elif event == "action":
        title = "执行动作"
        detail = (
            f"phase={data.get('phase')} {_fmt_action(data.get('action') or {})} "
            f"extra={data.get('extra') or {}}"
        )
    elif event == "llm_enqueued":
        kind = data.get("kind") or "-"
        title = f"提交LLM请求[{kind}]"
        if kind == "nav":
            detail = (
                f"state={_short_sig(data.get('state_sig'))} "
                f"open_gaps={data.get('open_gaps_count')} "
                f"history={len(data.get('history') or [])}"
            )
        elif kind == "topic_fill":
            detail = (
                f"state={_short_sig(data.get('state_sig'))} "
                f"topic={data.get('topic_id')} pack_size={data.get('pack_size')}"
            )
        elif kind == "topic_route":
            detail = (
                f"state={_short_sig(data.get('state_sig'))} topic_count={data.get('topic_count')}"
            )
        else:
            detail = json.dumps(data, ensure_ascii=False)[:220]
    elif event == "llm_result":
        kind = data.get("kind") or "-"
        title = f"收到LLM结果[{kind}]"
        result = data.get("result") or {}
        if kind == "nav":
            detail = (
                f"state={_short_sig(data.get('state_sig'))} "
                f"ui_type={result.get('ui_type')} "
                f"overlay={result.get('overlay_kind')} "
                f"candidates={_candidate_summary(result)}"
            )
        elif kind == "topic_route":
            topics = result.get("relevant_topics") or []
            names = [t.get("topic_id") for t in topics[:4]]
            detail = (
                f"state={_short_sig(data.get('state_sig'))} "
                f"topics={names or []} "
                f"skip_reason={result.get('skip_reason')!r}"
            )
        elif kind == "topic_fill":
            detail = (
                f"state={_short_sig(data.get('state_sig'))} "
                f"topic={data.get('topic_id')} "
                f"updates={len((result.get('proposed_updates') or []))} "
                f"signals={result.get('detected_signals') or []}"
            )
        else:
            detail = json.dumps(data, ensure_ascii=False)[:220]
    elif event == "questionnaire_update":
        title = "问卷更新"
        detail = (
            f"state={_short_sig(data.get('state_sig'))} "
            f"topic={data.get('topic_id')} "
            f"updates={len((data.get('proposed_updates') or []))} "
            f"signals={data.get('detected_signals') or []}"
        )
    elif event == "decision":
        title = "内部决策"
        detail = json.dumps(data, ensure_ascii=False)[:220]
    elif event == "parse_error":
        title = "解析失败"
        detail = json.dumps(data, ensure_ascii=False)[:220]

    step_id = ctx.get("step_id")
    prefix = f"[step={step_id}] " if step_id is not None else ""
    return prefix + title, detail


def _print_timeline(rows: Iterable[Dict[str, Any]]) -> None:
    print("=== 动作时间线 ===")
    for row in rows:
        title, detail = _format_event(row)
        print(title)
        print(f"  {detail}")


FIELD_GUIDE = """
=== 结构说明 ===

trace.jsonl 是“每行一个 JSON 事件”的日志文件。
最外层最常见的字段有：

1. event
   事件类型。
   常见值：
   - snapshot: 抓到一个页面快照
   - transition: 状态变化/状态观察
   - action: 执行了一个动作
   - llm_enqueued: 发起一次 LLM 请求
   - llm_result: 收到一次 LLM 返回
   - questionnaire_update: 问卷更新结果
   - decision: 某些内部决策记录

2. ts
   事件发生时间戳（Unix 秒）。

3. ctx
   上下文信息，表示“这件事是在什么运行状态下发生的”。
   常见字段：
   - run_id: 本次运行 id
   - step_id: 全局步骤号，按时间递增
   - cur_sig: 当前页面状态签名
   - stack: 当前 DFS/导航栈
   - open_gaps: 问卷中还没补齐的问题
   - answered_ratio: 当前问卷完成比例

4. data
   该事件自己的详细内容。不同 event 的 data 结构不同。

=== 你最常看哪几类 event ===

1. snapshot
   看“当前页面被识别成什么样”。
   关键字段：
   - data.state_sig: 当前页面状态签名
   - data.screenshot_path: 截图路径
   - data.uist_path: 结构化 UI 路径
   - data.vid_map_summary: 这页有哪些可引用元素
   - data.meta.xml_reliable: XML 是否可靠
   - data.meta.foreground_package/activity: 当前前台页面

2. action
   看“实际执行了什么动作”。
   关键字段：
   - data.phase: before / after
   - data.action.action: click / back / input 等
   - data.action.element_id: 点击的是哪个元素 id
   - data.extra.success: 动作是否成功

3. transition
   看“动作后页面有没有变”。
   关键字段：
   - data.src: 动作前状态
   - data.dst: 动作后状态
   - data.action.outcome.changed: 页面是否变化
   - data.action.outcome.change_type: 变化类型（nav 等）

4. llm_result(kind=nav)
   看“LLM 怎么理解当前页面，以及建议下一步做什么”。
   关键字段：
   - data.result.ui_type: 页面类型
   - data.result.page_summary: 页面概述
   - data.result.candidate_actions: 候选动作
   - data.result.key_interactables: 重点元素 id

5. questionnaire_update / llm_result(kind=topic_fill)
   看“有没有真的推进问卷答案”。
   关键字段：
   - topic_id: 哪个主题
   - proposed_updates: 新建议的答案
   - detected_signals: 从当前页面看到了什么线索
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze framework trace.jsonl in a readable form.")
    p.add_argument("trace_path", help="Path to trace.jsonl")
    p.add_argument("--out", default=None, help="Optional output txt path")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    path = Path(args.trace_path).resolve()
    rows = _load_jsonl(path)

    out_lines: List[str] = []
    out_lines.append(f"TRACE: {path}")
    out_lines.append(f"TOTAL_EVENTS: {len(rows)}")
    counts: Dict[str, int] = {}
    for row in rows:
        event = row.get("event") or "unknown"
        counts[event] = counts.get(event, 0) + 1
    out_lines.append("=== 事件统计 ===")
    for k in sorted(counts):
        out_lines.append(f"{k}: {counts[k]}")
    out_lines.append("")
    out_lines.append(FIELD_GUIDE.strip())
    out_lines.append("")
    out_lines.append("=== 动作时间线 ===")
    for row in rows:
        title, detail = _format_event(row)
        out_lines.append(title)
        out_lines.append(f"  {detail}")

    output = "\n".join(out_lines)
    print(output)

    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(f"\nsaved analysis to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
