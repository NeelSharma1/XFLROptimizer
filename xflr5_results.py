"""Parsing, metric extraction, and stability checks for XFLR5 results.

The XFLR5 polar exporter writes a small metadata preamble followed by either
CSV or fixed-width/whitespace data.  A stability *operating point* exported as
text additionally contains the eight longitudinal/lateral eigenvalues.  This
module deliberately keeps those two inputs separate so a normal polar cannot
accidentally be treated as proof of dynamic stability.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Iterable, Mapping, Sequence


_FLOAT_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _clean_header(value: str) -> str:
    """Normalize known XFLR headers without merging ``CL`` and ``Cl``."""

    cleaned = value.strip().lstrip("\ufeff").replace("\N{GREEK SMALL LETTER ALPHA}", "alpha")
    cleaned = cleaned.replace("\N{GREEK SMALL LETTER BETA}", "beta")
    compact = re.sub(r"\s+", "", cleaned)
    known = {
        "Alpha": "alpha",
        "alpha": "alpha",
        "AoA": "alpha",
        "Beta": "beta",
        "beta": "beta",
        "CL": "CL",
        "CD": "CD",
        "CDi": "CDi",
        "CDv": "CDv",
        "CY": "CY",
        "Cy": "CY",
        "Cl": "Cl",
        "Cm": "Cm",
        "Cn": "Cn",
        "Cni": "Cni",
        "QInf": "QInf",
        "VInf": "QInf",
        "XCP": "XCP",
        "Ctrl": "control",
        "Control": "control",
    }
    return known.get(compact, cleaned.strip())


def parse_result_table(result_file: str | Path) -> list[dict[str, float]]:
    """Read an XFLR5 CSV/TXT polar table.

    Metadata before the table and trailing operating-point details are ignored.
    Non-finite values are rejected because they would otherwise poison an
    optimizer score while still looking like a successful XFLR export.
    """

    path = Path(result_file)
    lines = [line.rstrip() for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()]
    for index, raw_header in enumerate(lines[:-1]):
        header_line = raw_header.strip()
        if not header_line:
            continue
        delimiter = next((candidate for candidate in (",", ";", "\t") if candidate in header_line), None)
        raw_headers = header_line.split(delimiter) if delimiter else header_line.split()
        headers = [_clean_header(value) for value in raw_headers]
        if len(headers) < 2 or not any(header in {"alpha", "beta", "CL", "CD", "Cm", "Cn"} for header in headers):
            continue
        if len(set(headers)) != len(headers):
            continue

        rows: list[dict[str, float]] = []
        for raw_row in lines[index + 1 :]:
            row_line = raw_row.strip()
            if not row_line:
                if rows:
                    break
                continue
            values = row_line.split(delimiter) if delimiter else row_line.split()
            if len(values) != len(headers):
                if rows:
                    break
                continue
            try:
                numeric_values = [value.strip().replace("\N{DEGREE SIGN}", "") for value in values]
                if delimiter != ",":
                    numeric_values = [
                        value.replace(",", ".") if "," in value and "." not in value else value
                        for value in numeric_values
                    ]
                parsed_values = [float(value) for value in numeric_values]
            except ValueError:
                if rows:
                    break
                continue
            if not all(math.isfinite(value) for value in parsed_values):
                raise ValueError(f"Non-finite numeric data was found in {path}.")
            rows.append(dict(zip(headers, parsed_values)))
        if rows:
            return rows
    raise ValueError(f"Could not find a numeric XFLR5 result table in {path}.")


def _row_value(row: Mapping[str, float], requested: str) -> float:
    if requested in row:
        return float(row[requested])
    folded = requested.casefold()
    matches = [float(value) for key, value in row.items() if key.casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(requested)


def linear_slope(
    rows: Sequence[Mapping[str, float]],
    response: str,
    independent: str,
    *,
    per_radian: bool = True,
    around_zero: float | None = None,
) -> float:
    """Return an ordinary least-squares derivative from a sweep."""

    pairs: list[tuple[float, float]] = []
    for row in rows:
        try:
            x_value = _row_value(row, independent)
            y_value = _row_value(row, response)
        except KeyError:
            continue
        if around_zero is None or abs(x_value) <= around_zero:
            pairs.append((x_value, y_value))
    if len(pairs) < 2:
        raise ValueError(f"At least two {independent}/{response} points are required for a derivative.")
    x_mean = fmean(x for x, _ in pairs)
    y_mean = fmean(y for _, y in pairs)
    denominator = sum((x - x_mean) ** 2 for x, _ in pairs)
    if denominator <= 0:
        raise ValueError(f"The {independent} sweep has no variation.")
    slope_per_degree = sum((x - x_mean) * (y - y_mean) for x, y in pairs) / denominator
    return slope_per_degree * (180.0 / math.pi) if per_radian else slope_per_degree


def interpolate_zero(
    rows: Sequence[Mapping[str, float]],
    response: str,
    independent: str,
) -> tuple[float, Mapping[str, float]]:
    """Linearly locate ``response == 0`` and interpolate every numeric column."""

    ordered = sorted(rows, key=lambda row: _row_value(row, independent))
    for row in ordered:
        if _row_value(row, response) == 0:
            return _row_value(row, independent), dict(row)
    for left, right in zip(ordered, ordered[1:]):
        y0, y1 = _row_value(left, response), _row_value(right, response)
        if y0 * y1 > 0 or y0 == y1:
            continue
        fraction = -y0 / (y1 - y0)
        keys = left.keys() & right.keys()
        interpolated = {key: float(left[key]) + fraction * (float(right[key]) - float(left[key])) for key in keys}
        return _row_value(left, independent) + fraction * (
            _row_value(right, independent) - _row_value(left, independent)
        ), interpolated
    raise ValueError(f"The sweep does not bracket {response}=0.")


@dataclass(frozen=True)
class MetricContext:
    air_density: float = 1.225
    reference_area: float | None = None
    default_speed: float | None = None


@dataclass(frozen=True)
class ModalMode:
    direction: str
    name: str
    real: float
    imaginary: float

    @property
    def eigenvalue(self) -> complex:
        return complex(self.real, self.imaginary)

    @property
    def frequency_hz(self) -> float:
        return abs(self.imaginary) / (2.0 * math.pi)

    @property
    def damping_ratio(self) -> float:
        magnitude = math.hypot(self.real, self.imaginary)
        return 1.0 if magnitude == 0 and self.real < 0 else (-self.real / magnitude if magnitude else 0.0)

    @property
    def time_to_double_seconds(self) -> float | None:
        return math.log(2.0) / self.real if self.real > 0 else None


@dataclass(frozen=True)
class ModalAnalysis:
    modes: tuple[ModalMode, ...]

    @property
    def max_real_part(self) -> float:
        if not self.modes:
            raise ValueError("Modal analysis contains no modes.")
        return max(mode.real for mode in self.modes)


def _mode_names(direction: str, values: Sequence[complex]) -> list[str]:
    """Classify XFLR's conventional four roots per direction."""

    if direction == "longitudinal":
        nonzero = sorted({round(abs(value.imag), 12) for value in values if abs(value.imag) > 1e-10})
        if len(nonzero) >= 2:
            split = (nonzero[0] + nonzero[-1]) / 2.0
            return ["phugoid" if abs(value.imag) <= split else "short_period" for value in values]
        ordered = sorted(range(len(values)), key=lambda index: abs(values[index].real))
        names = ["short_period"] * len(values)
        for index in ordered[: max(1, len(values) // 2)]:
            names[index] = "phugoid"
        return names

    oscillatory = [index for index, value in enumerate(values) if abs(value.imag) > 1e-10]
    names = ["dutch_roll" if index in oscillatory else "lateral" for index in range(len(values))]
    real_indices = [index for index in range(len(values)) if index not in oscillatory]
    if real_indices:
        ordered = sorted(real_indices, key=lambda index: abs(values[index].real))
        names[ordered[0]] = "spiral"
        for index in ordered[1:]:
            names[index] = "roll"
    return names


def parse_modal_report(report_file: str | Path) -> ModalAnalysis:
    """Parse the two ``Eigenvalue:`` lines in an XFLR5 op-point TXT export."""

    path = Path(report_file)
    direction: str | None = None
    grouped: dict[str, list[complex]] = {"longitudinal": [], "lateral": []}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        lowered = line.casefold()
        if "longitudinal" in lowered and "mode" in lowered:
            direction = "longitudinal"
            continue
        if "lateral" in lowered and "mode" in lowered:
            direction = "lateral"
            continue
        if direction is None or "eigenvalue" not in lowered:
            continue
        segments = line.split(":", maxsplit=1)[-1].split("|")
        for segment in segments:
            numbers = [float(value) for value in _FLOAT_RE.findall(segment)]
            if len(numbers) >= 2:
                grouped[direction].append(complex(numbers[0], numbers[1]))

    modes: list[ModalMode] = []
    for direction in ("longitudinal", "lateral"):
        values = grouped[direction]
        if len(values) != 4:
            raise ValueError(
                f"Expected four {direction} eigenvalues in {path}, found {len(values)}. "
                "Export the stability operating point as TXT, not the polar as CSV."
            )
        for value, name in zip(values, _mode_names(direction, values)):
            modes.append(ModalMode(direction, name, value.real, value.imag))
    return ModalAnalysis(tuple(modes))


@dataclass(frozen=True)
class StabilityCriteria:
    """Engineering gates applied to alpha, beta, and modal results."""

    cm_alpha_max: float = -1.0e-6
    cn_beta_min: float = 1.0e-6
    cl_beta_max: float = -1.0e-6
    trim_cl_min: float = 0.0
    static_alpha_range: tuple[float, float] = (-4.0, 4.0)
    static_beta_range: tuple[float, float] = (-4.0, 4.0)
    modal_real_max: float = -1.0e-8
    allow_pilot_correctable: bool = True
    correctable_modes: tuple[str, ...] = ("spiral", "phugoid")
    min_time_to_double_seconds: float = 5.0
    available_controls: tuple[str, ...] = ("pitch", "roll", "yaw")
    required_control_axes: tuple[str, ...] = ("pitch", "roll", "yaw")
    require_modal: bool = True

    def __post_init__(self) -> None:
        numeric = (
            self.cm_alpha_max,
            self.cn_beta_min,
            self.cl_beta_max,
            self.trim_cl_min,
            self.modal_real_max,
            self.min_time_to_double_seconds,
            *self.static_alpha_range,
            *self.static_beta_range,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("Stability criteria must contain only finite numbers.")
        if self.min_time_to_double_seconds < 0:
            raise ValueError("min_time_to_double_seconds cannot be negative.")
        for name, bounds in (
            ("static_alpha_range", self.static_alpha_range),
            ("static_beta_range", self.static_beta_range),
        ):
            if len(bounds) != 2 or bounds[0] >= bounds[1]:
                raise ValueError(f"{name} must be an increasing [minimum, maximum] pair.")
        known_modes = {"phugoid", "short_period", "dutch_roll", "roll", "spiral", "lateral"}
        unknown_modes = {value.casefold() for value in self.correctable_modes} - known_modes
        if unknown_modes:
            raise ValueError(f"Unknown pilot-correctable mode(s): {', '.join(sorted(unknown_modes))}.")
        unknown_controls = {
            value.casefold()
            for value in (*self.available_controls, *self.required_control_axes)
        } - {"pitch", "roll", "yaw"}
        if unknown_controls:
            raise ValueError(f"Unknown pilot control axis/axes: {', '.join(sorted(unknown_controls))}.")


@dataclass
class StabilityAssessment:
    static_longitudinal: bool
    static_directional: bool
    static_lateral: bool
    trimmed_positive_lift: bool
    dynamic_stable: bool
    pilot_controllable: bool
    modal_complete: bool
    control_axes_available: bool = True
    modal_required: bool = True
    metrics: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        static_ok = self.static_longitudinal and self.static_directional and self.static_lateral
        dynamic_ok = not self.modal_required or (
            self.modal_complete and (self.dynamic_stable or self.pilot_controllable)
        )
        return static_ok and self.trimmed_positive_lift and self.control_axes_available and dynamic_ok

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"feasible": self.feasible}


def _controls_for_mode(name: str) -> set[str]:
    return {
        "phugoid": {"pitch"},
        "short_period": {"pitch"},
        "spiral": {"roll", "yaw"},
        "roll": {"roll"},
        "dutch_roll": {"roll", "yaw"},
    }.get(name, set())


def assess_stability(
    alpha_rows: Sequence[Mapping[str, float]],
    beta_rows: Sequence[Mapping[str, float]],
    modal: ModalAnalysis | None,
    criteria: StabilityCriteria = StabilityCriteria(),
) -> StabilityAssessment:
    """Check static derivatives, trim, modal damping, and pilot-correctability.

    A slowly divergent phugoid or spiral is only considered pilot-correctable
    when the configured control set can affect that mode and its time to double
    exceeds the configured reaction/correction allowance.  This is a screening
    gate, not a substitute for a handling-qualities flight test.
    """

    metrics: dict[str, float] = {}
    reasons: list[str] = []
    controls = {control.casefold() for control in criteria.available_controls}
    required_controls = {control.casefold() for control in criteria.required_control_axes}
    control_axes_available = required_controls.issubset(controls)
    try:
        static_alpha_rows = _conditioned_rows(alpha_rows, {"alpha": criteria.static_alpha_range})
        metrics["CM_ALPHA"] = linear_slope(static_alpha_rows, "Cm", "alpha")
        longitudinal = metrics["CM_ALPHA"] <= criteria.cm_alpha_max
    except (KeyError, ValueError) as error:
        longitudinal = False
        reasons.append(f"Longitudinal static derivative unavailable: {error}")
    try:
        static_beta_rows = _conditioned_rows(beta_rows, {"beta": criteria.static_beta_range})
        metrics["CN_BETA"] = linear_slope(static_beta_rows, "Cn", "beta")
        directional = metrics["CN_BETA"] >= criteria.cn_beta_min
    except (KeyError, ValueError) as error:
        directional = False
        reasons.append(f"Directional static derivative unavailable: {error}")
    try:
        metrics["CL_ROLL_BETA"] = linear_slope(static_beta_rows, "Cl", "beta")
        lateral = metrics["CL_ROLL_BETA"] <= criteria.cl_beta_max
    except (KeyError, ValueError) as error:
        lateral = False
        reasons.append(f"Lateral static derivative unavailable: {error}")
    try:
        trim_alpha, trim_row = interpolate_zero(alpha_rows, "Cm", "alpha")
        metrics["TRIM_ALPHA"] = trim_alpha
        metrics["TRIM_CL"] = _row_value(trim_row, "CL")
        trimmed = metrics["TRIM_CL"] >= criteria.trim_cl_min
    except (KeyError, ValueError) as error:
        trimmed = False
        reasons.append(f"Positive-lift trim unavailable: {error}")

    modal_complete = modal is not None and len(modal.modes) == 8
    if not modal_complete:
        dynamic_stable = False
        pilot_controllable = False
        if criteria.require_modal:
            reasons.append("Eight-mode XFLR stability operating-point evidence is missing.")
    else:
        assert modal is not None
        metrics["MAX_MODE_REAL"] = modal.max_real_part
        dynamic_stable = all(mode.real <= criteria.modal_real_max for mode in modal.modes)
        unstable = [mode for mode in modal.modes if mode.real > criteria.modal_real_max]
        correctable_names = {name.casefold() for name in criteria.correctable_modes}
        pilot_controllable = bool(criteria.allow_pilot_correctable)
        for mode in unstable:
            time_to_double = mode.time_to_double_seconds or 0.0
            needed_controls = _controls_for_mode(mode.name)
            if (
                mode.name.casefold() not in correctable_names
                or time_to_double < criteria.min_time_to_double_seconds
                or not (needed_controls & controls)
            ):
                pilot_controllable = False
                reasons.append(
                    f"Unstable {mode.name} mode (real={mode.real:.5g}, doubling={time_to_double:.3g}s) "
                    "is outside the configured pilot-correctable envelope."
                )
        if not unstable:
            pilot_controllable = control_axes_available

    if not longitudinal:
        reasons.append("Cm_alpha does not satisfy the longitudinal static-stability limit.")
    if not directional:
        reasons.append("Cn_beta does not satisfy the directional static-stability limit.")
    if not lateral:
        reasons.append("Cl_beta does not satisfy the lateral static-stability limit.")
    if not trimmed:
        reasons.append("No Cm=0 operating point with the required positive lift was established.")
    if not control_axes_available:
        missing_controls = ", ".join(sorted(required_controls - controls))
        reasons.append(f"Required pilot control axis/axes are not configured: {missing_controls}.")
    return StabilityAssessment(
        longitudinal,
        directional,
        lateral,
        trimmed,
        dynamic_stable,
        pilot_controllable,
        modal_complete,
        control_axes_available,
        criteria.require_modal,
        metrics,
        reasons,
    )


def validate_sweep_coverage(
    rows: Sequence[Mapping[str, float]],
    independent: str,
    expected_values: Sequence[float],
    *,
    tolerance: float,
) -> None:
    """Reject a partial/misidentified XFLR sequence before it reaches scoring."""

    if tolerance <= 0 or not math.isfinite(tolerance):
        raise ValueError("Sweep validation tolerance must be finite and positive.")
    actual: list[float] = []
    for row in rows:
        try:
            actual.append(_row_value(row, independent))
        except KeyError:
            continue
    missing = [
        expected
        for expected in expected_values
        if not any(math.isclose(value, expected, rel_tol=0.0, abs_tol=tolerance) for value in actual)
    ]
    if missing:
        preview = ", ".join(f"{value:g}" for value in missing[:8])
        suffix = "..." if len(missing) > 8 else ""
        raise ValueError(
            f"The {independent} sweep is incomplete; {len(missing)} requested point(s) are missing: "
            f"{preview}{suffix}."
        )


def _conditioned_rows(
    rows: Sequence[Mapping[str, float]],
    conditions: Mapping[str, Sequence[float] | float] | None,
) -> list[Mapping[str, float]]:
    if not conditions:
        return list(rows)
    selected: list[Mapping[str, float]] = []
    for row in rows:
        keep = True
        for key, requested in conditions.items():
            try:
                value = _row_value(row, key)
            except KeyError:
                keep = False
                break
            if isinstance(requested, (int, float)):
                keep = math.isclose(value, float(requested), rel_tol=0.0, abs_tol=1e-9)
            else:
                bounds = list(requested)
                if len(bounds) != 2:
                    raise ValueError(f"Condition {key!r} must be a number or [minimum, maximum].")
                keep = float(bounds[0]) <= value <= float(bounds[1])
            if not keep:
                break
        if keep:
            selected.append(row)
    return selected


def _canonical_metric(metric: str) -> str:
    raw = metric.strip()
    exact = {"CL": "CL", "Cl": "Cl", "CD": "CD", "Cm": "Cm", "Cn": "Cn", "CY": "CY"}
    if raw in exact:
        return exact[raw]
    normalized = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    aliases = {
        "lift": "LIFT",
        "lift_force": "LIFT",
        "drag": "DRAG",
        "drag_force": "DRAG",
        "lift_coefficient": "CL",
        "coefficient_of_lift": "CL",
        "drag_coefficient": "CD",
        "coefficient_of_drag": "CD",
        "side_force_coefficient": "CY",
        "roll_moment_coefficient": "Cl",
        "pitch_moment_coefficient": "Cm",
        "yaw_moment_coefficient": "Cn",
        "lift_to_drag": "CL/CD",
        "l_d": "CL/CD",
        "ld": "CL/CD",
        "cm_alpha": "CM_ALPHA",
        "cmalpha": "CM_ALPHA",
        "cl_alpha": "CL_ALPHA",
        "clalpha": "CL_ALPHA",
        "cn_beta": "CN_BETA",
        "cnbeta": "CN_BETA",
        "cl_beta": "CL_ROLL_BETA",
        "clbeta": "CL_ROLL_BETA",
        "cy_beta": "CY_BETA",
        "cybeta": "CY_BETA",
        "static_margin": "STATIC_MARGIN",
        "trim_alpha": "TRIM_ALPHA",
        "trim_cl": "TRIM_CL",
        "max_mode_real": "MAX_MODE_REAL",
        "minimum_damping_ratio": "MIN_DAMPING_RATIO",
        "min_damping_ratio": "MIN_DAMPING_RATIO",
        "minimum_time_to_double": "MIN_TIME_TO_DOUBLE",
        "min_time_to_double": "MIN_TIME_TO_DOUBLE",
    }
    if normalized in aliases:
        return aliases[normalized]
    upper = raw.upper().replace(" ", "")
    if upper in {"CL/CD", "CDI", "CDV", "XCP", "QINF"}:
        return {"CDI": "CDi", "CDV": "CDv", "XCP": "XCP", "QINF": "QInf"}.get(upper, upper)
    return raw


def metric_requires_modal(metric: str) -> bool:
    """Return whether an objective can only be evaluated from modal roots."""

    return _canonical_metric(metric) in {"MAX_MODE_REAL", "MIN_DAMPING_RATIO", "MIN_TIME_TO_DOUBLE"}


def metric_values(
    metric: str,
    *,
    alpha_rows: Sequence[Mapping[str, float]],
    beta_rows: Sequence[Mapping[str, float]],
    modal: ModalAnalysis | None = None,
    sweep: str = "alpha",
    context: MetricContext = MetricContext(),
    conditions: Mapping[str, Sequence[float] | float] | None = None,
) -> list[float]:
    """Return raw values for a direct or derived optimization metric."""

    canonical = _canonical_metric(metric)
    source_rows = alpha_rows if sweep.casefold() == "alpha" else beta_rows
    rows = _conditioned_rows(source_rows, conditions)
    if not rows and canonical not in {"MAX_MODE_REAL", "MIN_DAMPING_RATIO", "MIN_TIME_TO_DOUBLE"}:
        raise ValueError(f"No {sweep} rows satisfy the objective conditions.")

    derivative_map = {
        "CM_ALPHA": (alpha_rows, "Cm", "alpha"),
        "CL_ALPHA": (alpha_rows, "CL", "alpha"),
        "CN_BETA": (beta_rows, "Cn", "beta"),
        "CL_ROLL_BETA": (beta_rows, "Cl", "beta"),
        "CY_BETA": (beta_rows, "CY", "beta"),
    }
    if canonical in derivative_map:
        derivative_rows, response, independent = derivative_map[canonical]
        conditioned = _conditioned_rows(derivative_rows, conditions)
        return [linear_slope(conditioned, response, independent)]
    if canonical == "STATIC_MARGIN":
        cl_alpha = linear_slope(_conditioned_rows(alpha_rows, conditions), "CL", "alpha")
        cm_alpha = linear_slope(_conditioned_rows(alpha_rows, conditions), "Cm", "alpha")
        if cl_alpha == 0:
            raise ValueError("CL_alpha is zero; static margin cannot be calculated.")
        return [-cm_alpha / cl_alpha]
    if canonical in {"TRIM_ALPHA", "TRIM_CL"}:
        trim_alpha, trim_row = interpolate_zero(_conditioned_rows(alpha_rows, conditions), "Cm", "alpha")
        return [trim_alpha if canonical == "TRIM_ALPHA" else _row_value(trim_row, "CL")]
    if canonical in {"MAX_MODE_REAL", "MIN_DAMPING_RATIO", "MIN_TIME_TO_DOUBLE"}:
        if modal is None or not modal.modes:
            raise ValueError(f"{canonical} requires an XFLR modal operating-point report.")
        if canonical == "MAX_MODE_REAL":
            return [modal.max_real_part]
        if canonical == "MIN_DAMPING_RATIO":
            return [min(mode.damping_ratio for mode in modal.modes)]
        times = [mode.time_to_double_seconds for mode in modal.modes if mode.time_to_double_seconds is not None]
        # A fully stable modal set has no doubling event. Use a documented,
        # finite engineering sentinel so optimization checkpoints stay valid JSON.
        return [min(times) if times else 1.0e12]

    values: list[float] = []
    for row in rows:
        if canonical == "CL/CD":
            denominator = _row_value(row, "CD")
            if denominator != 0:
                values.append(_row_value(row, "CL") / denominator)
        elif canonical in {"LIFT", "DRAG"}:
            if context.reference_area is None or context.reference_area <= 0:
                raise ValueError(f"{canonical.title()} requires a positive reference_area in square metres.")
            try:
                speed = _row_value(row, "QInf")
            except KeyError:
                if context.default_speed is None:
                    raise ValueError(f"{canonical.title()} requires QInf data or a default analysis speed.")
                speed = context.default_speed
            coefficient = _row_value(row, "CL" if canonical == "LIFT" else "CD")
            values.append(0.5 * context.air_density * speed * speed * context.reference_area * coefficient)
        else:
            values.append(_row_value(row, canonical))
    if not values:
        raise ValueError(f"Metric {metric!r} is unavailable in the selected {sweep} data.")
    return values


def reduce_metric(values: Iterable[float], reducer: str = "max") -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        raise ValueError("Metric contains no finite values.")
    operation = reducer.strip().casefold()
    if operation == "max":
        return max(finite)
    if operation == "min":
        return min(finite)
    if operation in {"mean", "average"}:
        return fmean(finite)
    if operation in {"absmax", "max_abs"}:
        return max(finite, key=abs)
    if operation in {"absmin", "min_abs"}:
        return min(finite, key=abs)
    if operation in {"last", "final"}:
        return finite[-1]
    raise ValueError(f"Unsupported metric reducer {reducer!r}.")


__all__ = [
    "MetricContext",
    "ModalAnalysis",
    "ModalMode",
    "StabilityAssessment",
    "StabilityCriteria",
    "assess_stability",
    "interpolate_zero",
    "linear_slope",
    "metric_requires_modal",
    "metric_values",
    "parse_modal_report",
    "parse_result_table",
    "reduce_metric",
    "validate_sweep_coverage",
]
