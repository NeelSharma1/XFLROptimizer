"""Run every test and emit immediate human- and machine-readable results."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.successes: list[str] = []
        self.skipped_records: list[tuple[str, str]] = []

    def addSuccess(self, test):
        self.successes.append(self.getDescription(test))
        super().addSuccess(test)

    def addSkip(self, test, reason):
        self.skipped_records.append((self.getDescription(test), reason))
        super().addSkip(test, reason)


def run_tests(output_directory: str | Path, pattern: str = "test*.py") -> dict[str, object]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent), pattern=pattern)
    stream = io.StringIO()
    application_output = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult)
    with contextlib.redirect_stdout(application_output), contextlib.redirect_stderr(application_output):
        result: RecordingResult = runner.run(suite)  # type: ignore[assignment]
    status = "PASS" if result.wasSuccessful() else "FAIL"
    payload: dict[str, object] = {
        "status": status,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "tests_run": result.testsRun,
        "passed": len(result.successes),
        "failed": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped_records),
        "failures_detail": [
            {"test": result.getDescription(test), "traceback": traceback}
            for test, traceback in result.failures
        ],
        "errors_detail": [
            {"test": result.getDescription(test), "traceback": traceback}
            for test, traceback in result.errors
        ],
        "skipped_detail": [
            {"test": test, "reason": reason} for test, reason in result.skipped_records
        ],
    }
    transcript = stream.getvalue()
    if application_output.getvalue():
        transcript += "\nCaptured application output:\n" + application_output.getvalue()
    (output / "test_results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output / "TEST_RESULTS.txt").write_text(
        f"TEST STATUS: {status}\n"
        f"Run: {result.testsRun}; passed: {len(result.successes)}; failed: {len(result.failures)}; "
        f"errors: {len(result.errors)}; skipped: {len(result.skipped_records)}\n\n"
        f"{transcript}",
        encoding="utf-8",
    )
    print(f"TEST STATUS: {status}")
    print(
        f"Run {result.testsRun}: {len(result.successes)} passed, {len(result.failures)} failed, "
        f"{len(result.errors)} errors, {len(result.skipped_records)} skipped."
    )
    print(f"Detailed test report: {(output / 'TEST_RESULTS.txt').resolve()}")
    if not result.wasSuccessful():
        print(transcript)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("test_output"))
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    result = run_tests(args.output, args.pattern)
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
