"""One-command test, demonstration optimization, and report workflow."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

import Revamped_XFLRPlaneScript as application


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("runs"))
    parser.add_argument(
        "--config",
        type=Path,
        help="Run this real/demo JSON configuration after the tests instead of the built-in demo.",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open REPORT.html after a successful run.")
    args = parser.parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_directory = (args.output_root / f"run_{timestamp}").resolve()
    tests_directory = run_directory / "tests"
    run_directory.mkdir(parents=True, exist_ok=False)

    completed = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("run_test_suite.py")), "--output", str(tests_directory)],
        check=False,
    )
    test_file = tests_directory / "test_results.json"
    if completed.returncode != 0 or not test_file.is_file():
        (run_directory / "RUN_STATUS.txt").write_text(
            "FAIL\nThe test suite failed. Open tests/TEST_RESULTS.txt.\n", encoding="utf-8"
        )
        print(f"Run stopped because tests failed: {tests_directory / 'TEST_RESULTS.txt'}")
        raise SystemExit(1)
    test_summary = json.loads(test_file.read_text(encoding="utf-8"))
    try:
        if args.config is None:
            payload = application.default_demo_configuration()
            base_directory = Path(__file__).resolve().parent
        else:
            payload, base_directory = application._read_hybrid_configuration(args.config)
        payload["output"] = str(run_directory)
        _run, summary = application.run_hybrid_configuration(
            payload,
            base_directory=base_directory,
            output_override=run_directory,
            test_summary=test_summary,
        )
    except Exception as error:
        detail = traceback.format_exc()
        (run_directory / "RUN_ERROR.txt").write_text(detail, encoding="utf-8")
        (run_directory / "RUN_STATUS.txt").write_text(
            f"FAIL\nOptimization aborted: {type(error).__name__}: {error}\n"
            "Open RUN_ERROR.txt for the traceback and tests/TEST_RESULTS.txt for test results.\n",
            encoding="utf-8",
        )
        print(f"FULL RUN STATUS: FAIL\nOutput package: {run_directory}\nError report: {run_directory / 'RUN_ERROR.txt'}")
        raise SystemExit(2) from error
    report = run_directory / "REPORT.html"
    print(f"\nFULL RUN STATUS: {summary['status']}")
    print(f"Output package: {run_directory}")
    print(f"Open report: {report}")
    if summary["status"] != "PASS":
        raise SystemExit(2)
    if not args.no_open:
        webbrowser.open(report.as_uri())


if __name__ == "__main__":
    main()
