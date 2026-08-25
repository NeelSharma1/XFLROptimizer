"""Constrained XFLR5 GUI optimization.

XFLR5 cannot batch-run plane analyses through its scripting interface, so this
module deliberately uses its GUI. The automation is isolated in ``XFLRGui``;
everything else (candidate generation, XML editing, validation and scoring) is
ordinary Python and can be tested without XFLR5 installed.

Span values use the length units declared by the exported XFLR XML. For a
symmetric wing, span means full tip-to-tip span (twice the largest
``y_position``); otherwise it is max(y_position) - min(y_position).
"""

from __future__ import annotations

import json
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

try:
    import pyautogui as pg
    import pyperclip
except ImportError:  # XML tests and result processing work without GUI packages.
    pg = None
    pyperclip = None


class XFLRGuiError(RuntimeError):
    """Raised when the GUI is not in the expected XFLR5 state."""


class ConstraintError(ValueError):
    """Raised when a candidate violates the configured design restrictions."""


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


class PlaneXmlEditor:
    """Edit XFLR-exported plane or component XML without relying on line order."""

    @staticmethod
    def _wings(root: ET.Element, component_name: str | None = None) -> list[ET.Element]:
        wings = list(root.findall(".//wing"))
        if component_name is None:
            return wings
        return [wing for wing in wings if (wing.findtext("Name") or "").strip().lower() == component_name.lower()]

    @classmethod
    def _target_wing(cls, root: ET.Element, component_name: str | None = None) -> ET.Element:
        wings = cls._wings(root, component_name)
        if len(wings) != 1:
            description = component_name or "component"
            raise ConstraintError(f"Expected exactly one wing for {description!r}, found {len(wings)}.")
        return wings[0]

    @staticmethod
    def _selected_sections(wing: ET.Element, sections: tuple[int, int | None]) -> list[ET.Element]:
        start, end = sections
        if start < 1 or (end is not None and end < start):
            raise ConstraintError("sections must use one-indexed inclusive bounds, e.g. (1, None).")
        selected = list(wing.findall("./Sections/Section"))[start - 1 : end]
        if not selected:
            raise ConstraintError("The requested section range does not select any XML sections.")
        return selected

    @classmethod
    def replace_airfoils(
        cls,
        xml_path: str | Path,
        left_airfoil: str,
        right_airfoil: str | None = None,
        sections: tuple[int, int | None] = (1, None),
        component_name: str | None = None,
    ) -> None:
        tree = ET.parse(xml_path)
        wing = cls._target_wing(tree.getroot(), component_name)
        for section in cls._selected_sections(wing, sections):
            left = section.find("Left_Side_FoilName")
            right = section.find("Right_Side_FoilName")
            if left is None or right is None:
                raise ConstraintError("The selected XFLR section has no left/right airfoil fields.")
            left.text = left_airfoil
            right.text = right_airfoil or left_airfoil
        ET.indent(tree, space="    ")
        tree.write(xml_path, encoding="utf-8", xml_declaration=True)

    @classmethod
    def span(cls, xml_path: str | Path, component_name: str | None = None) -> float:
        tree = ET.parse(xml_path)
        wing = cls._target_wing(tree.getroot(), component_name)
        y_positions = [float(section.findtext("y_position")) for section in wing.findall("./Sections/Section")]
        if not y_positions:
            raise ConstraintError("The wing has no sections.")
        symmetric = (wing.findtext("Symetric") or "false").strip().lower() == "true"
        return 2 * max(abs(value) for value in y_positions) if symmetric else max(y_positions) - min(y_positions)

    @classmethod
    def set_span(cls, xml_path: str | Path, target_span: float, component_name: str | None = None) -> None:
        if target_span <= 0:
            raise ConstraintError("span must be positive.")
        current_span = cls.span(xml_path, component_name)
        if current_span <= 0:
            raise ConstraintError("Cannot scale a wing whose current span is zero.")
        tree = ET.parse(xml_path)
        wing = cls._target_wing(tree.getroot(), component_name)
        scale = target_span / current_span
        for section in wing.findall("./Sections/Section"):
            y_position = section.find("y_position")
            if y_position is None or y_position.text is None:
                raise ConstraintError("The selected XFLR section has no y_position.")
            y_position.text = f"{float(y_position.text) * scale:.6f}"
        ET.indent(tree, space="    ")
        tree.write(xml_path, encoding="utf-8", xml_declaration=True)


class XFLRGui:
    """The narrow, XFLR5-layout-specific GUI adapter used by the optimizer."""

    def __init__(
        self,
        project_file: str | Path,
        image_directory: str | Path | None = None,
        startup_timeout: float = 60.0,
        clear_existing_polars: bool = False,
    ) -> None:
        if pg is None or pyperclip is None:
            raise XFLRGuiError("Install pyautogui, pyperclip and opencv-python in the active Python environment.")
        self.project_file = Path(project_file)
        if not self.project_file.is_file():
            raise FileNotFoundError(self.project_file)
        self.image_directory = Path(image_directory or Path(__file__).with_name("images"))
        self.planelist: list[str] = []
        self.sequence_on = False
        self._wait_for_startup(startup_timeout)
        self.open_project_file(self.project_file)
        time.sleep(2)
        self.reset_to_plane_level()
        self.plane_location = self.find_and_click("planeArrow.png", x_fraction=5, click=False)
        if clear_existing_polars:
            self.delete_polars(self.plane_location)

    def _wait_for_startup(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pg.locate(str(self.image_directory / "start.png"), pg.screenshot(), confidence=0.95):
                return
            time.sleep(0.3)
        raise XFLRGuiError("Timed out waiting for XFLR5. Check display scaling and images/start.png.")

    @staticmethod
    def click(point: Sequence[float]) -> None:
        pg.click(*point)

    @staticmethod
    def enter_filename(filename: str | Path) -> None:
        time.sleep(0.5)
        pg.write(str(filename))
        pg.press("enter")

    @staticmethod
    def tab_sequence(tab_count: int, double_enter: bool = False) -> None:
        for _ in range(tab_count):
            time.sleep(0.05)
            pg.press("tab")
        pg.press("enter")
        if double_enter:
            pg.press("enter")

    def open_project_file(self, filepath: str | Path) -> None:
        self.click((18, 36))
        self.click((52, 81))
        self.enter_filename(filepath)

    def find_and_click(
        self,
        image_name: str,
        x_fraction: float = 0.5,
        y_fraction: float = 0.5,
        click: bool = True,
        confidence: float = 0.95,
    ) -> list[float]:
        time.sleep(0.3)
        box = pg.locate(str(self.image_directory / image_name), pg.screenshot(), confidence=confidence)
        if box is None:
            raise XFLRGuiError(f"Could not locate {image_name}; XFLR5 may not match the saved UI images.")
        point = [box.left + box.width * x_fraction, box.top + box.height * y_fraction]
        if click:
            self.click(point)
        return point

    @staticmethod
    def reset_to_object_level() -> None:
        pg.click(29, 129)

    @staticmethod
    def reset_to_plane_level() -> None:
        pg.click(40, 129)

    @staticmethod
    def key_from_part(part: str) -> str:
        keys = {"wing": "w", "rudder": "f", "elevator": "e", "body": "b"}
        try:
            return keys[part.strip().lower()]
        except KeyError as error:
            raise ValueError(f"Unsupported XFLR part: {part!r}") from error

    def export_wing(self, plane: Sequence[float], filepath: str | Path, part: str) -> None:
        self.click(plane)
        pg.hotkey("ctrl", self.key_from_part(part))
        self.find_and_click("otherButton.png")
        self.tab_sequence(1)
        self.enter_filename(filepath)
        self.tab_sequence(2)
        time.sleep(1)

    def import_wing(self, plane: Sequence[float], filepath: str | Path, part: str) -> None:
        self.click(plane)
        pg.hotkey("ctrl", self.key_from_part(part))
        self.find_and_click("otherButton.png")
        self.tab_sequence(2)
        self.enter_filename(filepath)
        self.tab_sequence(1)

    def replace_part_from_xml(self, plane: Sequence[float], filepath: str | Path, part: str) -> None:
        """Import a replacement and accept XFLR's default save action."""

        self.import_wing(plane, filepath, part)
        pg.press("enter")

    def swap_part_airfoil(
        self,
        plane: Sequence[float],
        part: str,
        left_airfoil: str,
        right_airfoil: str | None = None,
        xml_path: str | Path = "component.xml",
        sections: tuple[int, int | None] = (1, None),
    ) -> None:
        self.export_wing(plane, xml_path, part)
        PlaneXmlEditor.replace_airfoils(xml_path, left_airfoil, right_airfoil, sections)
        self.replace_part_from_xml(plane, xml_path, part)

    def alpha_or_beta_sweep(
        self,
        plane: Sequence[float],
        name: str,
        results_out: str | Path,
        speed: float = 70.0,
        sweep_start: float = -13.0,
        sweep_end: float = 13.0,
        sweep_increment: float = 0.5,
        fixed_angle: float = 0.0,
        wait_seconds: float = 20.0,
    ) -> None:
        self.click(plane)
        pg.press("f6")
        pg.write(str(speed))
        pg.press("tab")
        pg.write(str(fixed_angle))
        pg.press("tab")
        pg.press("space")
        pg.press("tab")
        pg.write(name)
        pg.press("enter", presses=2)
        if not self.sequence_on:
            self.find_and_click("sequenceOff.png", x_fraction=0.15, confidence=0.999)
            self.sequence_on = True
        coordinates = self.find_and_click("startAnalysis.png", x_fraction=2, click=False)
        for value in (sweep_start, sweep_end, sweep_increment):
            pg.tripleClick(*coordinates)
            pg.write(str(value))
            coordinates[1] += 25
        coordinates[1] += 30
        self.click(coordinates)
        time.sleep(wait_seconds)
        self.reset_to_plane_level()
        pg.doubleClick(*plane)
        analysis_location = self.find_analysis(name, plane)
        if analysis_location is None:
            raise XFLRGuiError(f"XFLR5 did not create analysis {name!r}.")
        self.click(analysis_location)
        pg.rightClick(*analysis_location)
        self.find_and_click("expRes.png")
        self.enter_filename(results_out)
        self.reset_to_plane_level()

    def copy_paste(self) -> str:
        pg.hotkey("ctrl", "c")
        return pyperclip.paste()

    def check_plane_name(self, coordinates: Sequence[float]) -> str:
        pg.doubleClick(*coordinates)
        pg.press("f2")
        name = self.copy_paste()
        pg.press("enter", presses=2)
        pg.doubleClick(*coordinates)
        return name

    def find_analysis(self, name: str, plane: Sequence[float], max_rows: int = 200) -> list[float] | None:
        location = [plane[0] + 10, plane[1] + 15]
        previous_name: str | None = None
        for _ in range(max_rows):
            self.click(location)
            pg.hotkey("shift", "f2")
            found_name = self.copy_paste()
            pg.press("escape")
            if name == found_name:
                return location
            if found_name == previous_name:
                return None
            previous_name = found_name
            location[1] += 15
        raise XFLRGuiError("Analysis tree traversal exceeded max_rows.")

    def plane_coordinates_from_index(self, index: int) -> list[float]:
        return [self.plane_location[0], self.plane_location[1] + 15 * index]

    def delete_polars(self, plane: Sequence[float]) -> None:
        pg.rightClick(*plane)
        self.find_and_click("delPolars.png")
        pg.press("enter")


# Preserve the original public class name for existing user scripts.
xflmacroscript = XFLRGui


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
        gui: XFLRGui | None,
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
        plane: Sequence[float],
        candidates: Iterable[AircraftCandidate],
        clear_polars_between_runs: bool = True,
        continue_on_error: bool = True,
    ) -> OptimizationResult:
        """Evaluate candidates and save a machine-readable checkpoint after each one."""

        if self.gui is None:
            raise RuntimeError("optimize() requires an XFLRGui instance.")
        result = OptimizationResult(maximize=self.maximize)
        baseline_path = self.work_directory / "baseline_component.xml"
        candidate_list = list(candidates)
        if not candidate_list:
            raise ValueError("No optimization candidates were supplied.")
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


def _interactive_settings() -> dict[str, object] | None:
    """Collect a complete run configuration from the terminal."""

    print("\nXFLR5 constrained aircraft optimizer")
    print("Paths may be absolute or relative to the current directory.\n")
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
        span_text = input("Span values to test, comma-separated (blank = baseline only): ").strip()
        try:
            spans: list[float | None] = [float(value) for value in _parse_csv_values(span_text)] or [None]
            if all(value is None or value > 0 for value in spans):
                break
        except ValueError:
            pass
        print("Enter positive numeric span values separated by commas.")
    span_limit = _prompt_float("Maximum full span, blank for no limit", optional=True)
    if span_limit is not None and span_limit <= 0:
        raise ValueError("The span limit must be positive.")

    print("\nAnalysis settings")
    analysis = AnalysisSpec(
        speed=_prompt_float("Speed", 70.0),
        sweep_start=_prompt_float("Sweep start", -13.0),
        sweep_end=_prompt_float("Sweep end", 13.0),
        sweep_increment=_prompt_float("Sweep increment", 0.5),
        fixed_angle=_prompt_float("Fixed angle", 0.0),
        wait_seconds=_prompt_float("Seconds to wait for XFLR5", 20.0),
    )
    metric = _prompt_text("Metric to maximize (column or ratio such as CL/CD)", "CL/CD")
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


def main() -> None:
    settings = _interactive_settings()
    if settings is None:
        return
    airfoils = settings["airfoils"]
    spans = settings["spans"]
    constraints = OptimizationConstraints(settings["span_limit"], tuple(airfoils))
    gui = XFLRGui(settings["project"])
    analysis = settings["analysis"]
    optimizer = AircraftOptimizer(
        gui,
        settings["output"],
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
    candidates = optimizer.grid_candidates(airfoils, spans, settings["part"])
    result = optimizer.optimize(
        gui.plane_coordinates_from_index(settings["plane_index"]),
        candidates,
        settings["clear_polars"],
    )
    if result.best is None:
        raise SystemExit("No candidate completed successfully; see optimization_results.json for errors.")
    print(f"Best candidate: {result.best.candidate.name} ({settings['metric']} = {result.best.score:g})")


if __name__ == "__main__":
    main()
