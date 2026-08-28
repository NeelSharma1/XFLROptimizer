"""Constrained XFLR5 GUI optimization.

XFLR5 cannot batch-run plane analyses through its scripting interface, so this
module deliberately uses its GUI. The automation is isolated in GUI adapter
modules; everything else (candidate generation, XML editing, validation and
scoring) is ordinary Python and can be tested without XFLR5 installed.

Span values use the length units declared by the exported plane/component XML.
For a symmetric wing, span means full tip-to-tip span (twice the largest
``y_position``); otherwise it is max(y_position) - min(y_position).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from xflr5_core import (
    ConstraintError,
    LengthUnitError,
    PlaneXmlEditor,
    XFLRGuiError,
    convert_length,
)
from xflr5_legacy_gui import XFLRGui, xflmacroscript
from xflr5_windows_gui import UiaLocator, WindowsPlaneRef, WindowsXFLRGui


@dataclass(frozen=True)
class OptimizationConstraints:
    """Restrictions applied before a candidate is sent to XFLR5.

    ``allowed_airfoils`` contains XFLR airfoil names, which normally are the
    first line of their corresponding ``.dat`` files.
    """

    span_limit: float | None = None
    allowed_airfoils: tuple[str, ...] = ()
    require_airfoil_files: bool = True


@dataclass(frozen=True)
class AnalysisSpec:
    """Analysis inputs and their XFLR5 units."""

    # Speed is m/s; all sweep and fixed angles are degrees; wait is seconds.
    speed: float = 70.0
    sweep_start: float = -13.0
    sweep_end: float = 13.0
    sweep_increment: float = 0.5
    fixed_angle: float = 0.0
    wait_seconds: float = 20.0


@dataclass(frozen=True)
class AircraftCandidate:
    """One candidate to evaluate without accumulating prior candidate changes."""

    name: str
    part: str = "wing"
    left_airfoil: str = ""
    right_airfoil: str | None = None
    span: float | None = None
    sections: tuple[int, int | None] = (1, None)

    @property
    def effective_right_airfoil(self) -> str:
        return self.right_airfoil or self.left_airfoil


@dataclass
class OptimizationRecord:
    candidate: AircraftCandidate
    status: str
    score: float | None = None
    result_file: str | None = None
    message: str | None = None


@dataclass
class OptimizationResult:
    records: list[OptimizationRecord] = field(default_factory=list)
    maximize: bool = True

    @property
    def successful(self) -> list[OptimizationRecord]:
        return [record for record in self.records if record.status == "ok" and record.score is not None]

    @property
    def best(self) -> OptimizationRecord | None:
        candidates = self.successful
        if not candidates:
            return None
        return max(candidates, key=lambda record: record.score) if self.maximize else min(
            candidates, key=lambda record: record.score
        )


def airfoils_in_directory(foil_directory: str | Path) -> set[str]:
    """Return XFLR airfoil names from every ``.dat`` file in a directory."""

    names: set[str] = set()
    for path in Path(foil_directory).glob("*.dat"):
        with path.open(encoding="utf-8", errors="replace") as source:
            name = source.readline().strip()
        if name:
            names.add(name)
    return names


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.")
    return safe or "candidate"


# The legacy adapter is implemented in xflr5_legacy_gui.py and re-exported here.



def parse_result_table(result_file: str | Path) -> list[dict[str, float]]:
    """Read the common CSV or whitespace result tables exported by XFLR5."""

    lines = [line.strip() for line in Path(result_file).read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        delimiter = next((value for value in (",", ";", "\t") if value in line), None)
        headers = [value.strip() for value in (line.split(delimiter) if delimiter else line.split())]
        if not headers or not any(re.search(r"[A-Za-z]", header) for header in headers):
            continue
        rows: list[dict[str, float]] = []
        for row_line in lines[index + 1 :]:
            values = [value.strip() for value in (row_line.split(delimiter) if delimiter else row_line.split())]
            if len(values) != len(headers):
                if rows:
                    break
                continue
            try:
                rows.append({header: float(value) for header, value in zip(headers, values)})
            except ValueError:
                if rows:
                    break
        if rows:
            return rows
    raise ValueError(f"Could not find a numeric result table in {result_file}.")


def score_result(result_file: str | Path, metric: str = "CL/CD", maximize: bool = True) -> float:
    """Score a result file by the best row for a column or ratio such as ``CL/CD``."""

    rows = parse_result_table(result_file)
    normalized_metric = metric.strip().lower().replace(" ", "")
    values: list[float] = []
    for row in rows:
        normalized = {key.lower().replace(" ", ""): value for key, value in row.items()}
        if normalized_metric in normalized:
            values.append(normalized[normalized_metric])
            continue
        if "/" in normalized_metric:
            numerator, denominator = normalized_metric.split("/", maxsplit=1)
            if numerator in normalized and denominator in normalized and normalized[denominator] != 0:
                values.append(normalized[numerator] / normalized[denominator])
    if not values:
        raise ValueError(f"Metric {metric!r} was not present in {result_file}.")
    return max(values) if maximize else min(values)


class AircraftOptimizer:
    """Grid-search optimizer using XFLR5's GUI for each feasible candidate."""

    def __init__(
        self,
        gui: Any | None,
        work_directory: str | Path,
        constraints: OptimizationConstraints,
        foil_directory: str | Path | None = None,
        metric: str = "CL/CD",
        maximize: bool = True,
        scorer: Callable[[str | Path], float] | None = None,
        speed: float = 70.0,
        sweep_start: float = -13.0,
        sweep_end: float = 13.0,
        sweep_increment: float = 0.5,
        fixed_angle: float = 0.0,
        wait_seconds: float = 20.0,
    ) -> None:
        self.gui = gui
        self.work_directory = Path(work_directory)
        self.work_directory.mkdir(parents=True, exist_ok=True)
        self.constraints = constraints
        self.analysis = AnalysisSpec(speed, sweep_start, sweep_end, sweep_increment, fixed_angle, wait_seconds)
        self.foil_directory = Path(foil_directory) if foil_directory else None
        self.metric = metric
        self.maximize = maximize
        self.scorer = scorer or (lambda result_file: score_result(result_file, metric, maximize))
        self.available_airfoils = airfoils_in_directory(self.foil_directory) if self.foil_directory else set()

    @staticmethod
    def grid_candidates(
        airfoils: Iterable[str],
        spans: Iterable[float | None] = (None,),
        part: str = "wing",
        sections: tuple[int, int | None] = (1, None),
    ) -> list[AircraftCandidate]:
        candidates = []
        for airfoil in airfoils:
            for span in spans:
                suffix = "baseline-span" if span is None else f"span-{span:g}"
                candidates.append(AircraftCandidate(f"{_safe_name(airfoil)}-{suffix}", part, airfoil, span=span, sections=sections))
        return candidates

    def validate(self, candidate: AircraftCandidate) -> None:
        selected = {candidate.left_airfoil, candidate.effective_right_airfoil}
        allowed = set(self.constraints.allowed_airfoils)
        if allowed and not selected.issubset(allowed):
            forbidden = ", ".join(sorted(selected - allowed))
            raise ConstraintError(f"Candidate uses airfoil(s) outside allowed_airfoils: {forbidden}.")
        if self.constraints.require_airfoil_files and self.foil_directory:
            missing = selected - self.available_airfoils
            if missing:
                raise ConstraintError(f"No .dat file was found for: {', '.join(sorted(missing))}.")
        if candidate.span is not None and candidate.span <= 0:
            raise ConstraintError("Candidate span must be positive.")
        if self.constraints.span_limit is not None and candidate.span is not None and candidate.span > self.constraints.span_limit:
            raise ConstraintError(f"Candidate span {candidate.span:g} exceeds limit {self.constraints.span_limit:g}.")

    def _candidate_paths(self, candidate: AircraftCandidate) -> tuple[Path, Path]:
        stem = _safe_name(candidate.name)
        return self.work_directory / f"{stem}.xml", self.work_directory / f"{stem}.csv"

    def _enforce_realized_span_limit(self, xml_path: Path) -> None:
        """Apply the span limit even when a candidate keeps the baseline span."""

        if self.constraints.span_limit is None:
            return
        realized_span = PlaneXmlEditor.span(xml_path)
        if realized_span > self.constraints.span_limit:
            raise ConstraintError(
                f"Realized span {realized_span:g} exceeds limit {self.constraints.span_limit:g}."
            )

    def optimize(
        self,
        plane: Any,
        candidates: Iterable[AircraftCandidate],
        clear_polars_between_runs: bool = True,
        continue_on_error: bool = True,
        baseline_path: str | Path | None = None,
    ) -> OptimizationResult:
        """Evaluate candidates and save a machine-readable checkpoint after each one."""

        if self.gui is None:
            raise RuntimeError("optimize() requires an XFLRGui instance.")
        result = OptimizationResult(maximize=self.maximize)
        baseline_path = Path(baseline_path) if baseline_path is not None else self.work_directory / "baseline_component.xml"
        candidate_list = list(candidates)
        if not candidate_list:
            raise ValueError("No optimization candidates were supplied.")
        if not baseline_path.is_file():
            self.gui.export_wing(plane, baseline_path, candidate_list[0].part)
        for candidate in candidate_list:
            xml_path, result_path = self._candidate_paths(candidate)
            try:
                self.validate(candidate)
                if candidate.part != candidate_list[0].part:
                    raise ConstraintError("Each optimize() call must operate on one aircraft part.")
                shutil.copy2(baseline_path, xml_path)
                PlaneXmlEditor.replace_airfoils(xml_path, candidate.left_airfoil, candidate.effective_right_airfoil, candidate.sections)
                if candidate.span is not None:
                    PlaneXmlEditor.set_span(xml_path, candidate.span)
                self._enforce_realized_span_limit(xml_path)
                self.gui.replace_part_from_xml(plane, xml_path, candidate.part)
                self.gui.alpha_or_beta_sweep(
                    plane,
                    candidate.name,
                    result_path,
                    speed=self.analysis.speed,
                    sweep_start=self.analysis.sweep_start,
                    sweep_end=self.analysis.sweep_end,
                    sweep_increment=self.analysis.sweep_increment,
                    fixed_angle=self.analysis.fixed_angle,
                    wait_seconds=self.analysis.wait_seconds,
                )
                record = OptimizationRecord(candidate, "ok", self.scorer(result_path), str(result_path))
                if clear_polars_between_runs:
                    self.gui.delete_polars(plane)
            except Exception as error:
                record = OptimizationRecord(candidate, "failed", result_file=str(result_path), message=str(error))
                if not continue_on_error:
                    result.records.append(record)
                    self._write_checkpoint(result)
                    raise
            result.records.append(record)
            self._write_checkpoint(result)
        return result

    def _write_checkpoint(self, result: OptimizationResult) -> None:
        payload = {
            "metric": self.metric,
            "maximize": result.maximize,
            "constraints": asdict(self.constraints),
            "records": [
                {**asdict(record), "candidate": asdict(record.candidate)}
                for record in result.records
            ],
        }
        (self.work_directory / "optimization_results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _parse_csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("A value is required.")


def _prompt_float(label: str, default: float | None = None, optional: bool = False) -> float | None:
    while True:
        if optional and default is None:
            value = input(f"{label}: ").strip()
            if not value:
                return None
        else:
            value = _prompt_text(label, str(default) if default is not None else None)
        try:
            return float(value)
        except ValueError:
            print("Enter a number, or leave it blank when the value is optional.")


def _prompt_length(label: str, optional: bool = False) -> str | None:
    while True:
        value = input(f"{label}: ").strip()
        if not value and optional:
            return None
        try:
            if convert_length(value, 1.0) <= 0:
                raise LengthUnitError("length must be positive")
            return value
        except LengthUnitError as error:
            print(f"{error} Enter a value such as '5 feet', '1.5 m', or '60'.")


def _prompt_int(label: str, default: int = 0) -> int:
    while True:
        value = _prompt_text(label, str(default))
        try:
            parsed = int(value)
            if parsed >= 0:
                return parsed
        except ValueError:
            pass
        print("Enter a non-negative whole number.")


def _prompt_yes_no(label: str, default: bool = True) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{default_text}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Answer yes or no.")


def _prompt_existing_path(label: str, directory: bool = False) -> Path:
    while True:
        path = Path(_prompt_text(label)).expanduser()
        if (path.is_dir() if directory else path.is_file()):
            return path
        kind = "directory" if directory else "file"
        print(f"That {kind} does not exist: {path}")


_CONFIG_DEFAULTS = {
    "part": "wing",
    "plane_index": "0",
    "spans": "",
    "span_limit": "",
    "speed": "70.0",
    "sweep_start": "-13.0",
    "sweep_end": "13.0",
    "sweep_increment": "0.5",
    "fixed_angle": "0.0",
    "wait_seconds": "20.0",
    "metric": "CL/CD",
    "maximize": "yes",
    "clear_polars": "yes",
    "output": "optimization_output",
}
_CONFIG_REQUIRED = ("project", "foil_directory", "airfoils")
_CONFIG_ALIASES = {
    "project_file": "project",
    "foil_dir": "foil_directory",
    "airfoil_dir": "foil_directory",
    "airfoils_to_test": "airfoils",
    "plane_number": "plane_index",
    "span_values": "spans",
    "maximum_span": "span_limit",
    "max_span": "span_limit",
    "delete_polars": "clear_polars",
}
_CONFIG_KEYS = set(_CONFIG_DEFAULTS) | set(_CONFIG_REQUIRED)


def _select_optimizer_config_file() -> Path | None:
    """Open a standalone optimizer-config picker; return None on cancel/close."""

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        print("The configuration file dialog is unavailable; using terminal entry instead.")
        return None

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        selected = filedialog.askopenfilename(
            parent=root,
            title="Select XFLR5 optimizer configuration (Cancel for terminal input)",
            initialdir=str(Path.cwd()),
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
    except Exception:
        print("The configuration file dialog could not be opened; using terminal entry instead.")
        return None
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    if not selected:
        print("No configuration file selected; using terminal entry instead.")
        return None
    return Path(selected).expanduser()


def _read_optimizer_config(config_file: str | Path) -> dict[str, str]:
    """Read one ``key=value`` optimizer option per non-empty configuration line."""

    path = Path(config_file).expanduser()
    if not path.is_file():
        raise FileNotFoundError(path)
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError(f"Optimizer configuration {path} is not a UTF-8 text file.") from error
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(
                f"Optimizer configuration line {line_number} must use key=value format: {raw_line!r}."
            )
        key, value = (part.strip() for part in line.split("=", maxsplit=1))
        normalized_key = _CONFIG_ALIASES.get(key.lower().replace("-", "_"), key.lower().replace("-", "_"))
        if normalized_key not in _CONFIG_KEYS:
            supported = ", ".join(sorted(_CONFIG_KEYS))
            raise ValueError(f"Unknown optimizer configuration option {key!r} on line {line_number}. Use: {supported}.")
        if normalized_key in values:
            raise ValueError(f"Optimizer configuration option {normalized_key!r} is repeated on line {line_number}.")
        values[normalized_key] = value
    missing = [key for key in _CONFIG_REQUIRED if not values.get(key, "").strip()]
    if missing:
        raise ValueError(f"Optimizer configuration is missing required option(s): {', '.join(missing)}.")
    return values


def _config_path(values: dict[str, str], key: str, config_file: Path, directory: bool = False) -> Path:
    value = values.get(key, "").strip()
    if not value:
        raise ValueError(f"Optimizer configuration option {key!r} cannot be blank.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_file.parent / path
    expected = path.is_dir() if directory else path.is_file()
    if not expected:
        kind = "directory" if directory else "file"
        raise ValueError(f"Optimizer configuration option {key!r} does not identify an existing {kind}: {path}")
    return path


def _config_float(values: dict[str, str], key: str) -> float:
    try:
        return float(values.get(key, _CONFIG_DEFAULTS[key]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"Optimizer configuration option {key!r} must be a number.") from error


def _config_int(values: dict[str, str], key: str) -> int:
    try:
        parsed = int(values.get(key, _CONFIG_DEFAULTS[key]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"Optimizer configuration option {key!r} must be a non-negative whole number.") from error
    if parsed < 0:
        raise ValueError(f"Optimizer configuration option {key!r} must be non-negative.")
    return parsed


def _config_bool(values: dict[str, str], key: str) -> bool:
    value = values.get(key, _CONFIG_DEFAULTS[key]).strip().lower()
    if value in {"y", "yes", "true", "1", "on"}:
        return True
    if value in {"n", "no", "false", "0", "off"}:
        return False
    raise ValueError(f"Optimizer configuration option {key!r} must be yes/no, true/false, or 1/0.")


def _config_length(value: str, key: str, optional: bool = False) -> str | None:
    value = value.strip()
    if not value and optional:
        return None
    try:
        if convert_length(value, 1.0) <= 0:
            raise LengthUnitError("length must be positive")
    except LengthUnitError as error:
        raise ValueError(f"Optimizer configuration option {key!r} is invalid: {error}") from error
    return value


def _settings_from_optimizer_config(config_file: str | Path) -> dict[str, object]:
    """Convert a selected optimizer text configuration into normal run settings."""

    config_path = Path(config_file).expanduser()
    values = _read_optimizer_config(config_path)
    project = _config_path(values, "project", config_path)
    foil_directory = _config_path(values, "foil_directory", config_path, directory=True)
    available = sorted(airfoils_in_directory(foil_directory))
    if not available:
        raise ValueError(f"No .dat airfoils were found in {foil_directory}.")

    selected_text = values["airfoils"].strip()
    selected = available if selected_text.lower() == "all" else _parse_csv_values(selected_text)
    missing = sorted(set(selected) - set(available))
    if not selected or missing:
        unknown = ", ".join(missing) if missing else "no airfoils"
        raise ValueError(f"Optimizer configuration airfoils are invalid: {unknown}.")

    part = values.get("part", _CONFIG_DEFAULTS["part"]).strip().lower()
    if part not in {"wing", "elevator", "rudder"}:
        raise ValueError("Optimizer configuration option 'part' must be wing, elevator, or rudder.")
    plane_index = _config_int(values, "plane_index")

    spans_text = values.get("spans", _CONFIG_DEFAULTS["spans"])
    spans: list[str | None] = _parse_csv_values(spans_text) or [None]
    spans = [_config_length(value, "spans") if value is not None else None for value in spans]
    span_limit = _config_length(values.get("span_limit", _CONFIG_DEFAULTS["span_limit"]), "span_limit", optional=True)

    analysis = AnalysisSpec(
        speed=_config_float(values, "speed"),
        sweep_start=_config_float(values, "sweep_start"),
        sweep_end=_config_float(values, "sweep_end"),
        sweep_increment=_config_float(values, "sweep_increment"),
        fixed_angle=_config_float(values, "fixed_angle"),
        wait_seconds=_config_float(values, "wait_seconds"),
    )
    metric = values.get("metric", _CONFIG_DEFAULTS["metric"]).strip() or _CONFIG_DEFAULTS["metric"]
    output_value = values.get("output", _CONFIG_DEFAULTS["output"]).strip()
    if not output_value:
        raise ValueError("Optimizer configuration option 'output' cannot be blank.")
    output = Path(output_value).expanduser()
    if not output.is_absolute():
        output = config_path.parent / output

    print(f"Loaded optimizer configuration from {config_path}.")
    print(f"This will evaluate up to {len(selected) * len(spans)} candidate(s).")
    return {
        "project": project,
        "foil_directory": foil_directory,
        "airfoils": selected,
        "part": part,
        "plane_index": plane_index,
        "spans": spans,
        "span_limit": span_limit,
        "analysis": analysis,
        "metric": metric,
        "maximize": _config_bool(values, "maximize"),
        "clear_polars": _config_bool(values, "clear_polars"),
        "output": output,
    }


def _interactive_settings_terminal() -> dict[str, object] | None:
    """Collect a complete run configuration from the terminal."""

    print("\nXFLR5 constrained aircraft optimizer")
    print("Paths may be absolute or relative to the current directory.\n")
    print("Units: speed = m/s; sweep and fixed angles = degrees; wait time = seconds.")
    print("Geometry lengths use the native XFLR XML unit. The supplied XML files use inches")
    print("(0.0254 metres per unit); verify this in the selected aircraft XML before running.\n")
    project = _prompt_existing_path("XFLR5 project (.xfl)")
    foil_directory = _prompt_existing_path("Airfoil .dat directory", directory=True)
    available = sorted(airfoils_in_directory(foil_directory))
    if not available:
        raise ValueError(f"No .dat airfoils were found in {foil_directory}.")
    print("\nAvailable airfoils:")
    print("  " + ", ".join(available))
    while True:
        selected_text = _prompt_text("Airfoils to test (comma-separated, or 'all')")
        selected = available if selected_text.lower() == "all" else _parse_csv_values(selected_text)
        missing = sorted(set(selected) - set(available))
        if selected and not missing:
            break
        print("Unknown airfoil name(s): " + ", ".join(missing))

    part = _prompt_text("Component to optimize", "wing").lower()
    while part not in {"wing", "elevator", "rudder"}:
        print("Choose wing, elevator, or rudder.")
        part = _prompt_text("Component to optimize", "wing").lower()
    plane_index = _prompt_int("Plane number (zero-based)", 0)

    while True:
        span_text = input(
            "Span values to test, comma-separated [native XML length units; supplied files use inches] "
            "(blank = baseline only; units allowed, e.g. '5 feet'): "
        ).strip()
        try:
            spans: list[str | None] = _parse_csv_values(span_text) or [None]
            for value in spans:
                if value is not None and convert_length(value, 1.0) <= 0:
                    raise LengthUnitError("length must be positive")
            if spans:
                break
        except LengthUnitError as error:
            print(f"{error} Enter positive values with optional units, such as '5 feet, 20 in'.")
    span_limit = _prompt_length(
        "Maximum full span [native XML length units; supplied files use inches], blank for no limit",
        optional=True,
    )

    print("\nAnalysis settings")
    analysis = AnalysisSpec(
        speed=_prompt_float("Speed [m/s]", 70.0),
        sweep_start=_prompt_float("Sweep start angle [degrees]", -13.0),
        sweep_end=_prompt_float("Sweep end angle [degrees]", 13.0),
        sweep_increment=_prompt_float("Sweep increment [degrees]", 0.5),
        fixed_angle=_prompt_float("Fixed angle [degrees]", 0.0),
        wait_seconds=_prompt_float("Seconds to wait for XFLR5 [seconds]", 20.0),
    )
    metric = _prompt_text("Metric to maximize (CL/CD is dimensionless)", "CL/CD")
    maximize = _prompt_yes_no("Maximize this metric?", True)
    clear_polars = _prompt_yes_no("Delete plane polars between candidates?", True)
    output = Path(_prompt_text("Output directory", "optimization_output")).expanduser()

    candidate_count = len(selected) * len(spans)
    print(f"\nThis will evaluate up to {candidate_count} candidate(s).")
    if not _prompt_yes_no("Start XFLR5 optimization now?", True):
        print("Optimization cancelled.")
        return None
    return {
        "project": project,
        "foil_directory": foil_directory,
        "airfoils": selected,
        "part": part,
        "plane_index": plane_index,
        "spans": spans,
        "span_limit": span_limit,
        "analysis": analysis,
        "metric": metric,
        "maximize": maximize,
        "clear_polars": clear_polars,
        "output": output,
    }


def _interactive_settings() -> dict[str, object] | None:
    """Select an optimizer configuration file, or use the existing terminal interface on cancel."""

    print("\nSelect an optimizer configuration text file (Cancel or close for terminal input).")
    config_file = _select_optimizer_config_file()
    if config_file is not None:
        return _settings_from_optimizer_config(config_file)
    return _interactive_settings_terminal()


def _parse_cli_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the constrained XFLR5 aircraft optimizer.")
    parser.add_argument(
        "--gui-backend",
        choices=("auto", "windows", "legacy"),
        default="auto",
        help="GUI adapter to use; auto selects UI Automation on Windows and the legacy adapter elsewhere.",
    )
    parser.add_argument(
        "--xflr5-executable",
        type=Path,
        help="Optional Windows xflr5.exe path to launch when no running XFLR5 process is found.",
    )
    return parser.parse_args(argv)


def _make_gui(
    project_file: str | Path,
    backend: str = "auto",
    executable_path: str | Path | None = None,
) -> Any:
    selected = backend.strip().lower()
    if selected == "auto":
        selected = "windows" if sys.platform == "win32" else "legacy"
    if selected == "windows":
        if sys.platform != "win32":
            raise XFLRGuiError("The Windows UI Automation backend can only run on Windows.")
        from xflr5_windows_gui import WindowsXFLRGui

        return WindowsXFLRGui(project_file, executable_path=executable_path)
    if selected == "legacy":
        return XFLRGui(project_file)
    raise ValueError(f"Unsupported GUI backend {backend!r}; use auto, windows, or legacy.")


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_cli_args(argv)
    settings = _interactive_settings()
    if settings is None:
        return
    airfoils = settings["airfoils"]
    output = Path(settings["output"])
    output.mkdir(parents=True, exist_ok=True)
    gui = _make_gui(settings["project"], args.gui_backend, args.xflr5_executable)
    part = settings["part"]
    plane = gui.plane_coordinates_from_index(settings["plane_index"])
    baseline_path = output / "baseline_component.xml"
    gui.export_wing(plane, baseline_path, part)
    native_scale = PlaneXmlEditor.length_unit_to_meter(baseline_path)
    spans = [
        None if value is None else convert_length(value, native_scale)
        for value in settings["spans"]
    ]
    span_limit = settings["span_limit"]
    native_span_limit = None if span_limit is None else convert_length(span_limit, native_scale)
    print(f"Native XML length scale: {native_scale:g} metres per XFLR unit.")
    for entered, converted in zip(settings["spans"], spans):
        if entered is not None:
            print(f"  Span {entered} -> {converted:g} native XFLR units")
    if span_limit is not None:
        print(f"  Span limit {span_limit} -> {native_span_limit:g} native XFLR units")
    constraints = OptimizationConstraints(native_span_limit, tuple(airfoils))
    analysis = settings["analysis"]
    optimizer = AircraftOptimizer(
        gui,
        output,
        constraints,
        settings["foil_directory"],
        settings["metric"],
        settings["maximize"],
        speed=analysis.speed,
        sweep_start=analysis.sweep_start,
        sweep_end=analysis.sweep_end,
        sweep_increment=analysis.sweep_increment,
        fixed_angle=analysis.fixed_angle,
        wait_seconds=analysis.wait_seconds,
    )
    candidates = optimizer.grid_candidates(airfoils, spans, part)
    result = optimizer.optimize(
        plane,
        candidates,
        settings["clear_polars"],
        baseline_path=baseline_path,
    )
    if result.best is None:
        raise SystemExit("No candidate completed successfully; see optimization_results.json for errors.")
    print(f"Best candidate: {result.best.candidate.name} ({settings['metric']} = {result.best.score:g})")


if __name__ == "__main__":
    main()
