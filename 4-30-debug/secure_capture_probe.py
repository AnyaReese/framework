from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import WebDriverException

from appium_android import AndroidAppiumClient


@dataclass
class ProbeRecord:
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


def short_error(exc: Exception) -> str:
    text = str(exc).strip().replace("\n", " | ")
    return text[:500]


def now_ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def find_target_element(driver: Any, rid: str, text: str, timeout_s: float = 10.0):
    end = time.time() + timeout_s
    last_err = ""
    while time.time() < end:
        try:
            if rid:
                elems = driver.find_elements(AppiumBy.ID, rid)
                if elems:
                    return elems[0], f"id:{rid}"
            if text:
                xpath = f"//*[@text={json.dumps(text)}]"
                elems = driver.find_elements(AppiumBy.XPATH, xpath)
                if elems:
                    return elems[0], f"xpath_text:{text}"
        except Exception as e:
            last_err = short_error(e)
        time.sleep(0.4)
    raise RuntimeError(f"target element not found. rid={rid!r} text={text!r} last_err={last_err}")


def run_probe(args: argparse.Namespace) -> Dict[str, Any]:
    out_root = Path(args.output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    run_id = f"{now_ts()}_{args.package}"
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    appium = AndroidAppiumClient(server_url=args.appium_url, device_name=args.device_name).init_connection()
    driver = appium.driver
    assert driver is not None

    start = time.time()
    click_ok = False
    click_via = ""
    click_error = ""

    records: List[ProbeRecord] = []

    try:
        if args.relaunch:
            appium.force_stop(args.package)
            time.sleep(0.8)

        appium.ensure_foreground(args.package, args.activity if args.activity else None, wait=1.0)

        time.sleep(max(0.0, args.pre_click_wait_s))

        target, click_via = find_target_element(
            driver,
            rid=args.target_resource_id,
            text=args.target_text,
            timeout_s=args.find_timeout_s,
        )
        target.click()
        click_ok = True

        for i in range(args.probe_attempts):
            time.sleep(max(0.0, args.probe_interval_s))

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
                ps_hash = hashlib.md5(xml.encode("utf-8")).hexdigest() if xml else ""
                ps_ok = True
                if args.save_xml and xml:
                    (run_dir / f"probe_{i:02d}.xml").write_text(xml, encoding="utf-8")
            except Exception as e:
                ps_err = short_error(e)

            shot_ok = False
            shot_hash = ""
            shot_err = ""
            shot_kind = ""
            try:
                png = driver.get_screenshot_as_png()
                shot_hash = hashlib.md5(png).hexdigest() if png else ""
                shot_ok = True
                if args.save_screenshot and png:
                    (run_dir / f"probe_{i:02d}.png").write_bytes(png)
            except Exception as e:
                shot_err = short_error(e)
                shot_kind = classify_screenshot_error(shot_err)

            records.append(
                ProbeRecord(
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
    except Exception as e:
        click_error = short_error(e)
    finally:
        try:
            appium.quit()
        except Exception:
            pass

    screenshot_fail = [r for r in records if not r.screenshot_ok]
    secure_fail = [r for r in records if r.screenshot_error_kind == "secure_flag"]

    report = {
        "run_id": run_id,
        "package": args.package,
        "activity": args.activity,
        "appium_url": args.appium_url,
        "device_name": args.device_name,
        "click_ok": click_ok,
        "click_via": click_via,
        "click_error": click_error,
        "probe_attempts": args.probe_attempts,
        "probe_interval_s": args.probe_interval_s,
        "summary": {
            "records": len(records),
            "screenshot_fail_count": len(screenshot_fail),
            "secure_flag_count": len(secure_fail),
            "page_source_ok_count": sum(1 for r in records if r.page_source_ok),
            "screenshot_ok_count": sum(1 for r in records if r.screenshot_ok),
        },
        "records": [asdict(r) for r in records],
    }

    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "run_id": run_id,
        "report": str(report_path),
        "click_ok": click_ok,
        "screenshot_fail_count": len(screenshot_fail),
        "secure_flag_count": len(secure_fail),
    }, ensure_ascii=False))

    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reproduce post-click capture failures (secure screenshot suspect).")
    p.add_argument("--appium-url", default="http://127.0.0.1:4723")
    p.add_argument("--device-name", default="emulator-5554")
    p.add_argument("--package", default="com.netease.uu")
    p.add_argument("--activity", default="")
    p.add_argument("--target-resource-id", default="com.netease.uu:id/button")
    p.add_argument("--target-text", default="加速")
    p.add_argument("--find-timeout-s", type=float, default=12.0)
    p.add_argument("--relaunch", action="store_true")
    p.add_argument("--pre-click-wait-s", type=float, default=1.0)
    p.add_argument("--probe-attempts", type=int, default=10)
    p.add_argument("--probe-interval-s", type=float, default=0.3)
    p.add_argument("--save-xml", action="store_true")
    p.add_argument("--save-screenshot", action="store_true")
    p.add_argument("--output-dir", default="F:/workplace/framework/4-30-debug/runs")
    return p.parse_args()


if __name__ == "__main__":
    run_probe(parse_args())
