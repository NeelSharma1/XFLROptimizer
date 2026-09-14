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
import math
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from xflr5_core import (
    ConstraintError,
    LengthUnitError,
    PlaneXmlEditor,
    XFLRGuiError,
    convert_length,
)
from xflr5_legacy_gui import XFLRGui, xflmacroscript
from xflr5_windows_gui import UiaLocator, WindowsPlaneRef, WindowsXFLRGui
from xflr5_optimization import (
    AnalysisPlan,
    AnalyticDemoBackend,
    DesignVariable,
    GuiXFLRAnalysisBackend,
    HybridAircraftOptimizer,
    HybridSearchSettings,
    ObjectiveSpec,
    SweepDefinition,
    console_progress,
)
from xflr5_results import (
    MetricContext,
    ModalAnalysis,
    ModalMode,
    StabilityAssessment,
    StabilityCriteria,
    assess_stability,
    metric_requires_modal,
    metric_values,
    parse_modal_report,
    parse_result_table as _parse_xflr_result_table,
)


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

    return _parse_xflr_result_table(result_file)


def score_result(result_file: str | Path, metric: str = "CL/CD", maximize: bool = True) -> float:
    """Score a result file by the best row for a column or ratio such as ``CL/CD``."""

    rows = parse_result_table(result_file)
    requested = metric.strip().replace(" ", "")
    values: list[float] = []
    for row in rows:
        if requested in row:
            values.append(row[requested])
            continue
        if "/" in requested:
            numerator, denominator = requested.split("/", maxsplit=1)
            if numerator in row and denominator in row and row[denominator] != 0:
                values.append(row[numerator] / row[denominator])
                continue
        matches = [value for key, value in row.items() if key.casefold() == requested.casefold()]
        if len(matches) == 1:
            values.append(matches[0])
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
    parser = argparse.ArgumentParser(
        description="Run the hybrid Monte Carlo/gradient XFLR5 aircraft optimizer."
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="JSON optimization configuration. Paths inside it are relative to the configuration file.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the deterministic no-XFLR demo and generate the same complete output package.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Override the output directory in the JSON configuration or demo defaults.",
    )
    parser.add_argument(
        "--test-results",
        type=Path,
        help="Optional JSON test summary to embed in the final report.",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate and print the JSON optimizer settings without running XFLR5.",
    )
    parser.add_argument(
        "--legacy-interactive",
        action="store_true",
        help="Use the original one-metric interactive/grid-search workflow.",
    )
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


_HYBRID_CONFIG_KEYS = {
    "backend",
    "output",
    "project",
    "foil_directory",
    "baseline_component",
    "part",
    "plane_index",
    "sections",
    "gui_backend",
    "xflr5_executable",
    "variables",
    "objectives",
    "analysis",
    "stability",
    "search",
    "span_limit",
    "air_density",
    "reference_area_m2",
    "clear_polars_between_designs",
}


def default_demo_configuration() -> dict[str, object]:
    """Return a useful, deterministic full-pipeline configuration."""

    return {
        "backend": "demo",
        "output": "optimization_output",
        "variables": [
            {"name": "span", "minimum": 44.0, "maximum": 72.0, "initial": 60.0},
            {"name": "chord_scale", "minimum": 0.75, "maximum": 1.25, "initial": 1.0},
            {"name": "sweep_deg", "minimum": 0.0, "maximum": 35.0, "initial": 18.0},
            {"name": "tip_twist_deg", "minimum": -6.0, "maximum": 2.0, "initial": -2.0},
            {"name": "dihedral_deg", "minimum": 0.0, "maximum": 8.0, "initial": 4.0},
            {
                "name": "airfoil",
                "choices": ["N22", "NACA 2412", "NACA 23012"],
                "initial": "N22",
            },
        ],
        "objectives": [
            {
                "metric": "CL/CD",
                "goal": "maximize",
                "weight": 2.0,
                "scale": 30.0,
                "sweep": "alpha",
                "reducer": "max",
                "conditions": {"alpha": [-2.0, 10.0]},
                "label": "peak_efficiency",
            },
            {
                "metric": "Lift",
                "goal": "target",
                "target": 1800.0,
                "weight": 1.2,
                "scale": 500.0,
                "sweep": "alpha",
                "reducer": "max",
                "conditions": {"alpha": [0.0, 8.0]},
                "label": "lift_target_N",
            },
            {
                "metric": "Drag",
                "goal": "minimize",
                "weight": 0.8,
                "scale": 100.0,
                "sweep": "alpha",
                "reducer": "mean",
                "conditions": {"alpha": [0.0, 8.0]},
                "label": "mean_drag_N",
            },
            {
                "metric": "Cm alpha",
                "goal": "target",
                "target": -0.9,
                "weight": 1.0,
                "scale": 0.35,
                "sweep": "alpha",
                "reducer": "mean",
                "conditions": {"alpha": [-4.0, 8.0]},
                "label": "cm_alpha_per_rad",
            },
            {
                "metric": "Cn beta",
                "goal": "at_least",
                "target": 0.18,
                "weight": 1.0,
                "scale": 0.18,
                "sweep": "beta",
                "reducer": "mean",
                "hard": True,
                "label": "directional_stability",
            },
        ],
        "analysis": {
            "alpha": {"start": -8.0, "end": 12.0, "increment": 1.0, "speed": 70.0, "wait_seconds": 60.0},
            "beta": {"start": -8.0, "end": 8.0, "increment": 1.0, "speed": 70.0, "fixed_angle": 2.0, "wait_seconds": 60.0},
            "run_modal": True,
        },
        "stability": {
            "cm_alpha_max": -0.01,
            "cn_beta_min": 0.01,
            "cl_beta_max": -0.01,
            "trim_cl_min": 0.05,
            "static_alpha_range": [-4.0, 4.0],
            "static_beta_range": [-4.0, 4.0],
            "modal_real_max": -1e-08,
            "allow_pilot_correctable": True,
            "correctable_modes": ["spiral", "phugoid"],
            "min_time_to_double_seconds": 5.0,
            "available_controls": ["pitch", "roll", "yaw"],
            "required_control_axes": ["pitch", "roll", "yaw"],
            "require_modal": True,
        },
        "search": {
            "monte_carlo_samples": 12,
            "elite_count": 2,
            "local_iterations": 3,
            "learning_rate": 0.2,
            "gradient_fraction": 0.04,
            "convergence_tolerance": 0.0001,
            "random_seed": 2026,
            "max_evaluations": 80,
        },
    }


def _read_hybrid_configuration(config_file: str | Path) -> tuple[dict[str, object], Path]:
    path = Path(config_file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON configuration {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("The optimization JSON root must be an object.")
    unknown = set(payload) - _HYBRID_CONFIG_KEYS
    if unknown:
        raise ValueError(f"Unknown JSON configuration option(s): {', '.join(sorted(unknown))}.")
    return payload, path.parent


def _resolved_config_path(value: object, base_directory: Path, *, must_exist: bool = False, directory: bool = False) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_directory / path
    path = path.resolve()
    if must_exist and not (path.is_dir() if directory else path.is_file()):
        kind = "directory" if directory else "file"
        raise ValueError(f"Configured {kind} does not exist: {path}")
    return path


def _strict_config_boolean(payload: Mapping[str, object], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false.")
    return value


def _stability_criteria_from_dict(payload: Mapping[str, object]) -> StabilityCriteria:
    allowed = {field.name for field in StabilityCriteria.__dataclass_fields__.values()}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Unknown stability setting(s): {', '.join(sorted(unknown))}.")
    values = dict(payload)
    for name in ("correctable_modes", "available_controls", "required_control_axes"):
        if name in values:
            raw = values[name]
            if not isinstance(raw, (list, tuple)):
                raise ValueError(f"stability.{name} must be a JSON array.")
            values[name] = tuple(str(value) for value in raw)
    for name in ("static_alpha_range", "static_beta_range"):
        if name in values:
            raw = values[name]
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise ValueError(f"stability.{name} must be a two-number JSON array.")
            values[name] = (float(raw[0]), float(raw[1]))
    numeric_fields = {
        "cm_alpha_max",
        "cn_beta_min",
        "cl_beta_max",
        "trim_cl_min",
        "modal_real_max",
        "min_time_to_double_seconds",
    }
    for name in numeric_fields & values.keys():
        values[name] = float(values[name])
    for name in ("allow_pilot_correctable", "require_modal"):
        if name in values and not isinstance(values[name], bool):
            raise ValueError(f"stability.{name} must be true or false.")
    return StabilityCriteria(**values)  # type: ignore[arg-type]


def _hybrid_parts(payload: Mapping[str, object]) -> tuple[
    list[DesignVariable], list[ObjectiveSpec], AnalysisPlan, StabilityCriteria, HybridSearchSettings
]:
    unknown_root = set(payload) - _HYBRID_CONFIG_KEYS
    if unknown_root:
        raise ValueError(f"Unknown JSON configuration option(s): {', '.join(sorted(unknown_root))}.")
    raw_variables = payload.get("variables")
    raw_objectives = payload.get("objectives")
    if not isinstance(raw_variables, list) or not raw_variables or not all(isinstance(value, Mapping) for value in raw_variables):
        raise ValueError("Configuration 'variables' must be a non-empty JSON array of objects.")
    if not isinstance(raw_objectives, list) or not raw_objectives or not all(isinstance(value, Mapping) for value in raw_objectives):
        raise ValueError("Configuration 'objectives' must be a non-empty JSON array of objects.")
    variables = [DesignVariable.from_dict(value) for value in raw_variables]
    objectives = [ObjectiveSpec.from_dict(value) for value in raw_objectives]
    analysis_payload = payload.get("analysis", {}) or {}
    stability_payload = payload.get("stability", {}) or {}
    search_payload = payload.get("search", {}) or {}
    if not all(isinstance(value, Mapping) for value in (analysis_payload, stability_payload, search_payload)):
        raise ValueError("analysis, stability, and search settings must be JSON objects.")
    analysis = AnalysisPlan.from_dict(analysis_payload)
    stability = _stability_criteria_from_dict(stability_payload)
    search = HybridSearchSettings.from_dict(search_payload)
    if len({variable.name for variable in variables}) != len(variables):
        raise ValueError("Design variable names must be unique.")
    if len({objective.name for objective in objectives}) != len(objectives):
        raise ValueError("Objective labels/names must be unique.")
    if stability.require_modal and not analysis.run_modal:
        raise ValueError("stability.require_modal=true requires analysis.run_modal=true.")
    if not analysis.run_modal and any(metric_requires_modal(objective.metric) for objective in objectives):
        raise ValueError("A modal-root objective requires analysis.run_modal=true.")
    for name, definition, bounds in (
        ("static_alpha_range", analysis.alpha, stability.static_alpha_range),
        ("static_beta_range", analysis.beta, stability.static_beta_range),
    ):
        covered = [value for value in definition.values() if bounds[0] <= value <= bounds[1]]
        if len(covered) < 2:
            raise ValueError(f"stability.{name} must contain at least two requested sweep points.")
    backend_name = str(payload.get("backend", "xflr")).casefold()
    if backend_name not in {"demo", "xflr"}:
        raise ValueError("backend must be 'xflr' or 'demo'.")
    if "clear_polars_between_designs" in payload:
        _strict_config_boolean(payload, "clear_polars_between_designs", True)
    if backend_name == "xflr":
        unsupported = {
            variable.name
            for variable in variables
            if variable.name not in PlaneXmlEditor.SUPPORTED_GEOMETRY_PARAMETERS
            and variable.name not in GuiXFLRAnalysisBackend._AIRFOIL_KEYS
        }
        if unsupported:
            raise ValueError(f"Unsupported XFLR design variable(s): {', '.join(sorted(unsupported))}.")
        invalid_categories = {
            variable.name
            for variable in variables
            if variable.categorical and variable.name not in GuiXFLRAnalysisBackend._AIRFOIL_KEYS
        }
        if invalid_categories:
            raise ValueError(
                "Only airfoil variables can be categorical in the XFLR XML backend: "
                f"{', '.join(sorted(invalid_categories))}."
            )
        variable_names = {variable.name for variable in variables}
        incompatible = []
        if {"chord_scale", "root_chord"}.issubset(variable_names):
            incompatible.append("chord_scale with root_chord")
        if {"taper_ratio", "tip_chord"}.issubset(variable_names):
            incompatible.append("taper_ratio with tip_chord")
        if "airfoil" in variable_names and {"left_airfoil", "right_airfoil"} & variable_names:
            incompatible.append("airfoil with left_airfoil/right_airfoil")
        if incompatible:
            raise ValueError(
                "The XFLR design contains redundant/conflicting variable pairs: "
                f"{'; '.join(incompatible)}."
            )
    return variables, objectives, analysis, stability, search


def run_hybrid_configuration(
    payload: Mapping[str, object],
    *,
    base_directory: str | Path = ".",
    output_override: str | Path | None = None,
    gui_backend_override: str | None = None,
    executable_override: str | Path | None = None,
    test_summary: Mapping[str, object] | None = None,
) -> tuple[object, dict[str, object]]:
    """Validate, execute, and report one JSON-style hybrid configuration."""

    from xflr5_reporting import generate_output_package

    base = Path(base_directory).expanduser().resolve()
    variables, objectives, analysis, stability, search = _hybrid_parts(payload)
    output_value = output_override if output_override is not None else payload.get("output", "optimization_output")
    output = _resolved_config_path(output_value, base)
    backend_name = str(payload.get("backend", "xflr")).casefold()
    output.mkdir(parents=True, exist_ok=True)
    if backend_name == "demo":
        backend = AnalyticDemoBackend(output, analysis)
    elif backend_name == "xflr":
        if "project" not in payload:
            raise ValueError("The XFLR backend requires a project path.")
        project = _resolved_config_path(payload["project"], base, must_exist=True)
        selected_gui = gui_backend_override or str(payload.get("gui_backend", "auto"))
        executable_value = executable_override if executable_override is not None else payload.get("xflr5_executable")
        executable = None
        if executable_value:
            executable = _resolved_config_path(executable_value, base, must_exist=True)
        gui = _make_gui(project, selected_gui, executable)
        plane_index = int(payload.get("plane_index", 0))
        plane = gui.plane_coordinates_from_index(plane_index)
        part = str(payload.get("part", "wing")).casefold()
        if part not in {"wing", "elevator", "rudder"}:
            raise ValueError("part must be wing, elevator, or rudder.")
        if payload.get("baseline_component"):
            source_baseline = _resolved_config_path(payload["baseline_component"], base, must_exist=True)
            copied_baseline = output / "baseline_component.xml"
            if source_baseline.resolve() != copied_baseline.resolve():
                shutil.copy2(source_baseline, copied_baseline)
            baseline = copied_baseline
        else:
            baseline = output / "baseline_component.xml"
            baseline.unlink(missing_ok=True)
            gui.export_wing(plane, baseline, part)
        foil_directory = None
        available: set[str] = set()
        if payload.get("foil_directory"):
            foil_directory = _resolved_config_path(payload["foil_directory"], base, must_exist=True, directory=True)
            available = airfoils_in_directory(foil_directory)
        requested_airfoils = {
            choice
            for variable in variables
            if variable.name in {"airfoil", "left_airfoil", "right_airfoil"}
            for choice in variable.choices
        }
        if available and not requested_airfoils.issubset(available):
            missing = ", ".join(sorted(requested_airfoils - available))
            raise ValueError(f"Configured airfoil .dat file(s) were not found: {missing}.")
        sections_value = payload.get("sections", [1, None])
        if not isinstance(sections_value, (list, tuple)) or len(sections_value) != 2:
            raise ValueError("sections must be [one_based_start, inclusive_end_or_null].")
        sections = (int(sections_value[0]), None if sections_value[1] is None else int(sections_value[1]))
        if sections[0] < 1 or (sections[1] is not None and sections[1] < sections[0]):
            raise ValueError("sections must use increasing, one-based inclusive bounds.")
        air_density = float(payload.get("air_density", 1.225))
        reference_area = None if payload.get("reference_area_m2") is None else float(payload["reference_area_m2"])
        span_limit = None if payload.get("span_limit") is None else float(payload["span_limit"])
        if not math.isfinite(air_density) or air_density <= 0:
            raise ValueError("air_density must be finite and positive.")
        if reference_area is not None and (not math.isfinite(reference_area) or reference_area <= 0):
            raise ValueError("reference_area_m2 must be finite and positive when provided.")
        if span_limit is not None and (not math.isfinite(span_limit) or span_limit <= 0):
            raise ValueError("span_limit must be finite and positive when provided.")
        backend = GuiXFLRAnalysisBackend(
            gui,
            plane,
            baseline,
            output,
            analysis,
            part=part,
            sections=sections,
            allowed_airfoils=sorted(requested_airfoils),
            available_airfoils=sorted(available),
            span_limit=span_limit,
            stability_required=stability.require_modal,
            clear_polars_between_designs=_strict_config_boolean(
                payload, "clear_polars_between_designs", True
            ),
            air_density=air_density,
            reference_area_m2=reference_area,
        )
    else:
        raise ValueError("backend must be 'xflr' or 'demo'.")

    configuration_snapshot = json.loads(
        json.dumps(dict(payload), allow_nan=False, default=str)
    )
    configuration_snapshot["resolved_output_directory"] = str(output)
    (output / "configuration_snapshot.json").write_text(
        json.dumps(configuration_snapshot, indent=2, allow_nan=False), encoding="utf-8"
    )
    optimizer = HybridAircraftOptimizer(
        backend,
        variables,
        objectives,
        output,
        stability_criteria=stability,
        settings=search,
        progress=console_progress,
    )
    run = optimizer.optimize()
    summary = generate_output_package(
        run,
        output,
        configuration=configuration_snapshot,
        test_summary=test_summary,
    )
    return run, summary


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


def _run_legacy_interactive(args: argparse.Namespace) -> None:
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


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_cli_args(argv)
    if args.legacy_interactive or (args.config is None and not args.demo):
        _run_legacy_interactive(args)
        return
    if args.demo and args.config is not None:
        raise SystemExit("Use either --demo or --config, not both.")
    if args.demo:
        payload = default_demo_configuration()
        base_directory = Path(__file__).resolve().parent
    else:
        payload, base_directory = _read_hybrid_configuration(args.config)
    # Parse every typed section before reporting a configuration as valid.
    _hybrid_parts(payload)
    if args.validate_config:
        print(json.dumps(payload, indent=2))
        return
    tests: Mapping[str, object] | None = None
    if args.test_results is not None:
        if not args.test_results.is_file():
            raise SystemExit(f"Test-results JSON does not exist: {args.test_results}")
        loaded_tests = json.loads(args.test_results.read_text(encoding="utf-8"))
        if not isinstance(loaded_tests, dict):
            raise SystemExit("Test-results JSON root must be an object.")
        tests = loaded_tests
    run, summary = run_hybrid_configuration(
        payload,
        base_directory=base_directory,
        output_override=args.output,
        gui_backend_override=args.gui_backend,
        executable_override=args.xflr5_executable,
        test_summary=tests,
    )
    best = run.best
    output = Path(str(summary["output_directory"]))
    print(f"\nRun status: {summary['status']}")
    if best is not None:
        print(f"Selected design: {best.evaluation_id}; loss={best.loss:.6g}; feasible={best.feasible}")
    print(f"Complete report: {(output / 'REPORT.html').resolve()}")
    if summary["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
