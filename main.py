"""Entrypoint to run the Android UI exploration workflow."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from appium_android import AndroidAppiumClient
from gpt_cls import GPTClient
from questionnaire_state import QuestionnaireState
from workflow import BudgetConfig, WorkflowRunner
from trace_callbacks import InteractiveDebugCallbacks, JsonlTraceCallbacks, NoOpCallbacks

from dotenv import load_dotenv
load_dotenv()


def setup_logging(debug: bool, level: str, *, quiet_console: bool = False):
    # Console + file logging with optional quieter console mode for interactive stepping.
    root_level = logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] %(message)s')

    root = logging.getLogger()
    root.setLevel(root_level)
    root.handlers.clear()

    file_handler = logging.FileHandler('app.log', mode='a', encoding='utf-8')
    file_handler.setLevel(root_level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING if quiet_console else root_level)
    console_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)


def build_restart(appium: AndroidAppiumClient, package: str, activity: str, wait: float = 1.0):
    # Factory used in other tools: stops and relaunches the target package safely.
    def restart():
        try:
            appium.force_stop(package)
        except Exception:
            logging.getLogger(__name__).debug("force_stop failed; continuing")
        time.sleep(wait)
        appium.launch_app(package, activity)
    return restart


def parse_args(argv) -> argparse.Namespace:
    # CLI supports the main exploration run plus lightweight device utilities.
    p = argparse.ArgumentParser(description="Run Appium + LLM exploration workflow or device utilities.")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run exploration workflow")
    run.add_argument("--appium-url", type=str, default="http://127.0.0.1:4723", help="Appium server URL")
    run.add_argument("--device-name", type=str, default=None, help="Optional device name (caps deviceName)")
    run.add_argument("--package", type=str, required=True, help="Target app package name")
    run.add_argument("--activity", type=str, default=None, help="Optional launch activity")
    run.add_argument("--questionnaire-dir", type=str, required=True, help="Directory containing questionnaire JSON files")

    run.add_argument("--task", type=str, default="Explore the app to fill the questionnaire.", help="Exploration goal string")
    run.add_argument("--relaunch", action="store_true", help="Relaunch app each time and kill once finished")

    # Budgets
    run.add_argument("--time-budget", type=float, default=300.0, help="Total run time budget (seconds)")
    run.add_argument("--max-actions", type=int, default=300, help="Max number of actions (click/back/etc.)")
    run.add_argument("--probe-cap", type=int, default=10, help="Max candidate probes per page")
    run.add_argument("--disable-probe-return", action="store_true", help="Skip probe-return exploration and commit forward directly")
    run.add_argument("--min-candidate-score", type=float, default=-1.0, help="Filter out LLM1 candidates with score below this threshold before probe/forward")
    run.add_argument("--workers", type=int, default=4, help="Thread pool workers (LLM overlap)")

    # Model
    run.add_argument("--model", type=str, default="gpt-4o", help="OpenAI model name")
    run.add_argument("--temperature", type=float, default=0.2, help="LLM temperature")
    run.add_argument("--timeout", type=int, default=60, help="LLM request timeout seconds")
    run.add_argument("--api-key", type=str, default=None, help="OpenAI API key (or use OPENAI_API_KEY env)")
    run.add_argument("--trace-dir", type=str, default=None, help="If set, writes JSONL trace + assets to this directory")
    run.add_argument("--run-id", type=str, default=None, help="Optional run id (defaults to timestamp)")

    # Logging
    run.add_argument("--log-level", type=str, default="INFO", help="Logging level (DEBUG/INFO/WARNING)")
    run.add_argument("--debug", action="store_true")
    run.add_argument("--pause", action="store_true")
    run.add_argument(
        "--interactive-debug",
        action="store_true",
        help="Use concise Chinese step-by-step console output and wait for a key between major steps",
    )

    lp = sub.add_parser("list-packages", help="List installed packages")
    lp.add_argument("--device-name", default=None)
    lp.add_argument("--debug", action="store_true")

    ps = sub.add_parser("list-processes", help="List processes")
    ps.add_argument("--device-name", default=None)
    ps.add_argument("--debug", action="store_true")

    ss = sub.add_parser("screenshot", help="Capture screenshot to file")
    ss.add_argument("--device-name", default=None)
    ss.add_argument("--out", required=True)
    ss.add_argument("--debug", action="store_true")

    ui = sub.add_parser("dump-ui", help="Dump UI XML to file")
    ui.add_argument("--device-name", default=None)
    ui.add_argument("--out", required=True)
    ui.add_argument("--debug", action="store_true")

    ins = sub.add_parser("install-apk", help="Install APK via adbutils")
    ins.add_argument("--device-name", default=None)
    ins.add_argument("--apk", required=True)
    ins.add_argument("--debug", action="store_true")

    return p.parse_args(argv)

#============================  # Dev-default args for quick local run; comment out in production.
#package = "com.cs.cinemain"
# package = "com.android.settings"

# package = "com.calcitem.sanmill"
# package = "bim.app"
package = "com.marktka.calculatorYou"


sys.argv = [sys.argv[0], 
            "run", 
            # "--appium-url", "http://localhost:4723",
            "--appium-url", "http://127.0.0.1:4723",
            "--device-name", "emulator-5554",
            "--package", package,
            # "--questionnaire-dir", "./questionnaire-v2/others",
            "--questionnaire-dir", "./questionnaire-v2/games",
            "--trace-dir", "./traces/",
            "--run-id", time.strftime("%Y%m%d_%H%M%S") + "_" + package,
            #"--pause",
            "--interactive-debug",
            "--disable-probe-return",
            "--min-candidate-score", "0.0",
            "--relaunch", "--debug"]
#============================
def main(argv=None):
    # WHEN CALLED: process entry; sets up logging, loads questionnaires, initializes Appium/GPT, and calls WorkflowRunner.run.
    # POSITION: only place run() is invoked; other subcommands bypass workflow and perform utility actions.
    args = parse_args(argv or sys.argv[1:])
    setup_logging(
        getattr(args, "debug", False),
        getattr(args, "log_level", "INFO"),
        quiet_console=bool(getattr(args, "interactive_debug", False)),
    )
    logger = logging.getLogger(__name__)

    if args.cmd == "run":
        api_key = args.api_key or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            logging.getLogger(__name__).warning("OPENAI_API_KEY not set; GPT calls will fail.")

        # Load questionnaire(s)
        q = QuestionnaireState.load_from_dir(args.questionnaire_dir)

        # Init appium
        appium = AndroidAppiumClient(server_url=args.appium_url, device_name=args.device_name).init_connection()

        # Init GPT client
        gpt = GPTClient(api_key=api_key, model=args.model, temperature=args.temperature, timeout_s=args.timeout)

        budget = BudgetConfig(
            time_budget_s=float(args.time_budget),
            max_actions=int(args.max_actions),
            enable_probe_return=not bool(args.disable_probe_return),
            min_candidate_score=float(args.min_candidate_score),
            per_page_probe_cap=int(args.probe_cap),
            max_workers=int(args.workers),
        )

        if args.interactive_debug:
            callbacks = InteractiveDebugCallbacks()
        elif args.trace_dir:
            callbacks = JsonlTraceCallbacks(args.trace_dir, args.run_id)
        else:
            callbacks = NoOpCallbacks()

        runner = WorkflowRunner(
            appium=appium,
            gpt=gpt,
            questionnaires=q,
            budget=budget,
            target_package=args.package,
            target_activity=args.activity,
            pause=args.pause,
            callbacks=callbacks,
            run_id=getattr(callbacks, "run_id", args.run_id or ""),
        )

        if args.relaunch and args.package:
            appium.force_stop(args.package)

        try:
            runner.run(task=args.task)
        finally:
            if args.relaunch and args.package:
                appium.force_stop(args.package)
            appium.quit()

        # Export results (human-readable)
        results = q.export_answers_by_namespace()
        logger.info("Final answers by namespace:\n%s", results)

    else:
        from device_utils import list_packages, list_processes, screenshot, dump_ui, install_apk
        if args.cmd == "list-packages":
            for pkg in list_packages(args.device_name):
                print(pkg)
        elif args.cmd == "list-processes":
            for line in list_processes(args.device_name):
                print(line)
        elif args.cmd == "screenshot":
            screenshot(args.out, args.device_name)
            print(f"Saved screenshot to {args.out}")
        elif args.cmd == "dump-ui":
            dump_ui(args.out, args.device_name)
            print(f"Saved UI XML to {args.out}")
        elif args.cmd == "install-apk":
            install_apk(args.apk, args.device_name)
            print("APK installed")


if __name__ == "__main__":
    main()
