from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from appium_android import AndroidAppiumClient


@dataclass
class SampleRecord:
    index: int
    ts: float
    elapsed_s: float
    foreground_package: str
    foreground_activity: str
    page_source_ok: bool
    page_source_hash: str
    page_source_error: str
    screenshot_ok: bool
    screenshot_hash: str
    screenshot_error: str
    screenshot_error_kind: str


def short_error(exc: Exception) -> str:
    msg = str(exc).strip().replace("\n", " | ")
    return msg[:600]


def classify_screenshot_error(msg: str) -> str:
    m = (msg or "").lower()
    if "secure" in m and "screenshot" in m:
        return "secure_flag"
    if "failed to capture a screenshot" in m:
        return "capture_failed"
    if "no such session" in m:
        return "session_lost"
    if "timeout" in m:
        return "timeout"
    return "other"


def now_ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def run_probe(args: argparse.Namespace) -> Dict[str, Any]:
    out_root = Path(args.output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    run_id = f"{now_ts()}_{args.tag}"
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    appium = AndroidAppiumClient(server_url=args.appium_url, device_name=args.device_name).init_connection()
    driver = appium.driver
    assert driver is not None

    start = time.time()
    records: List[SampleRecord] = []

    try:
        time.sleep(max(0.0, args.warmup_s))
        for i in range(args.attempts):
            if i > 0:
                time.sleep(max(0.0, args.interval_s))

            fg_pkg = ""
            fg_act = ""
            try:
                fg_pkg = appium.foreground_package()
                fg_act = appium.foreground_activity()
            except Exception:
                pass

            ps_ok = False
            ps_hash = ""
            ps_err = ""
            try:
                xml = driver.page_source or ""
                ps_ok = True
                if xml:
                    ps_hash = hashlib.md5(xml.encode("utf-8")).hexdigest()
                if args.save_xml and xml:
                    (run_dir / f"sample_{i:02d}.xml").write_text(xml, encoding="utf-8")
            except Exception as e:
                ps_err = short_error(e)

            shot_ok = False
            shot_hash = ""
            shot_err = ""
            shot_kind = ""
            try:
                png = driver.get_screenshot_as_png()
                shot_ok = True
                if png:
                    shot_hash = hashlib.md5(png).hexdigest()
                if args.save_screenshot and png:
                    (run_dir / f"sample_{i:02d}.png").write_bytes(png)
            except Exception as e:
                shot_err = short_error(e)
                shot_kind = classify_screenshot_error(shot_err)

            records.append(
                SampleRecord(
                    index=i,
                    ts=time.time(),
                    elapsed_s=time.time() - start,
                    foreground_package=fg_pkg,
                    foreground_activity=fg_act,
                    page_source_ok=ps_ok,
                    page_source_hash=ps_hash,
                    page_source_error=ps_err,
                    screenshot_ok=shot_ok,
                    screenshot_hash=shot_hash,
                    screenshot_error=shot_err,
                    screenshot_error_kind=shot_kind,
                )
            )
    finally:
        try:
            appium.quit()
        except Exception:
            pass

    screenshot_fail = [r for r in records if not r.screenshot_ok]
    secure_flag = [r for r in records if r.screenshot_error_kind == "secure_flag"]
    page_source_fail = [r for r in records if not r.page_source_ok]

    report = {
        "run_id": run_id,
        "tag": args.tag,
        "appium_url": args.appium_url,
        "device_name": args.device_name,
        "attempts": args.attempts,
        "interval_s": args.interval_s,
        "summary": {
            "records": len(records),
            "page_source_ok_count": sum(1 for r in records if r.page_source_ok),
            "page_source_fail_count": len(page_source_fail),
            "screenshot_ok_count": sum(1 for r in records if r.screenshot_ok),
            "screenshot_fail_count": len(screenshot_fail),
            "secure_flag_count": len(secure_flag),
        },
        "records": [asdict(r) for r in records],
    }

    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "run_id": run_id,
                "report": str(report_path),
                "page_source_ok_count": report["summary"]["page_source_ok_count"],
                "screenshot_fail_count": report["summary"]["screenshot_fail_count"],
                "secure_flag_count": report["summary"]["secure_flag_count"],
            },
            ensure_ascii=False,
        )
    )
    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe currently opened page: page_source + screenshot only.")
    p.add_argument("--appium-url", default="http://127.0.0.1:4723")
    p.add_argument("--device-name", default="emulator-5554")
    p.add_argument("--tag", default="current_page")
    p.add_argument("--warmup-s", type=float, default=0.8)
    p.add_argument("--attempts", type=int, default=8)
    p.add_argument("--interval-s", type=float, default=0.35)
    p.add_argument("--save-xml", action="store_true")
    p.add_argument("--save-screenshot", action="store_true")
    p.add_argument("--output-dir", default="F:/workplace/framework/4-30-debug/runs")
    return p.parse_args()


if __name__ == "__main__":
    run_probe(parse_args())
