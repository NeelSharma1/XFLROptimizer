"""Hybrid Monte-Carlo/gradient optimization for XFLR5 aircraft designs."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from xflr5_core import ConstraintError, PlaneXmlEditor
from xflr5_results import (
    MetricContext,
    ModalAnalysis,
    StabilityAssessment,
    StabilityCriteria,
    assess_stability,
    metric_values,
    parse_modal_report,
    parse_result_table,
    reduce_metric,
    validate_sweep_coverage,
)


DesignValue = float | str
Design = dict[str, DesignValue]


def _json_boolean(payload: Mapping[str, object], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false.")
    return value


@dataclass(frozen=True)
class DesignVariable:
    """One bounded continuous variable or one categorical design choice."""

    name: str
    minimum: float | None = None
    maximum: float | None = None
    initial: DesignValue | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("A design variable name cannot be blank.")
        if self.choices:
            if self.minimum is not None or self.maximum is not None:
                raise ValueError(f"Categorical variable {self.name!r} cannot also have numeric bounds.")
            if len(set(self.choices)) != len(self.choices):
                raise ValueError(f"Categorical variable {self.name!r} contains duplicate choices.")
            if self.initial is not None and str(self.initial) not in self.choices:
                raise ValueError(f"Initial value for {self.name!r} is not one of its choices.")
            return
        if self.minimum is None or self.maximum is None:
            raise ValueError(f"Continuous variable {self.name!r} requires minimum and maximum.")
        if not (math.isfinite(self.minimum) and math.isfinite(self.maximum) and self.minimum < self.maximum):
            raise ValueError(f"Continuous bounds for {self.name!r} must be finite and increasing.")
        if self.initial is not None and not self.minimum <= float(self.initial) <= self.maximum:
            raise ValueError(f"Initial value for {self.name!r} is outside its bounds.")
        if self.step is not None and (not math.isfinite(self.step) or self.step <= 0):
            raise ValueError(f"Step for {self.name!r} must be finite and positive.")

    @property
    def categorical(self) -> bool:
        return bool(self.choices)

    @property
    def range(self) -> float:
        if self.categorical:
            raise TypeError(f"Categorical variable {self.name!r} has no numeric range.")
        assert self.minimum is not None and self.maximum is not None
        return self.maximum - self.minimum

    def default(self) -> DesignValue:
        if self.initial is not None:
            return self.initial
        if self.categorical:
            return self.choices[0]
        assert self.minimum is not None and self.maximum is not None
        return (self.minimum + self.maximum) / 2.0

    def project(self, value: DesignValue) -> DesignValue:
        if self.categorical:
            selected = str(value)
            if selected not in self.choices:
                raise ValueError(f"{selected!r} is not a valid choice for {self.name!r}.")
            return selected
        assert self.minimum is not None and self.maximum is not None
        projected = min(self.maximum, max(self.minimum, float(value)))
        if self.step is not None:
            projected = self.minimum + round((projected - self.minimum) / self.step) * self.step
            projected = min(self.maximum, max(self.minimum, projected))
        return float(projected)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "DesignVariable":
        allowed = {"name", "minimum", "maximum", "initial", "step", "choices"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"Unknown design-variable setting(s): {', '.join(sorted(unknown))}.")
        raw_choices = payload.get("choices", ()) or ()
        if not isinstance(raw_choices, (list, tuple)):
            raise ValueError("Design-variable choices must be a JSON array.")
        choices = tuple(str(value) for value in raw_choices)
        return cls(
            name=str(payload["name"]),
            minimum=None if payload.get("minimum") is None else float(payload["minimum"]),
            maximum=None if payload.get("maximum") is None else float(payload["maximum"]),
            initial=payload.get("initial"),
            step=None if payload.get("step") is None else float(payload["step"]),
            choices=choices,
        )


@dataclass(frozen=True)
class ObjectiveSpec:
    """A concurrent objective or one-sided metric requirement.

    Goals are ``maximize``, ``minimize``, ``target``, ``at_least``, and
    ``at_most``.  ``conditions`` filters sweep rows, for example
    ``{"alpha": [-2, 8]}``.  Set ``hard`` on at-least/at-most/target goals to
    reject designs which violate the requested tolerance.
    """

    metric: str
    goal: str = "maximize"
    target: float | None = None
    weight: float = 1.0
    scale: float | None = None
    sweep: str = "alpha"
    reducer: str = "max"
    conditions: Mapping[str, Sequence[float] | float] = field(default_factory=dict)
    hard: bool = False
    tolerance: float | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        goal = self.goal.casefold()
        if goal not in {"maximize", "minimize", "target", "at_least", "at_most"}:
            raise ValueError(f"Unsupported objective goal {self.goal!r}.")
        if goal in {"target", "at_least", "at_most"} and self.target is None:
            raise ValueError(f"Objective {self.metric!r} with goal {self.goal!r} requires target.")
        if self.target is not None and not math.isfinite(self.target):
            raise ValueError(f"Objective target for {self.metric!r} must be finite.")
        if self.weight <= 0 or not math.isfinite(self.weight):
            raise ValueError(f"Objective weight for {self.metric!r} must be finite and positive.")
        if self.scale is not None and (self.scale <= 0 or not math.isfinite(self.scale)):
            raise ValueError(f"Objective scale for {self.metric!r} must be finite and positive.")
        if self.tolerance is not None and (self.tolerance < 0 or not math.isfinite(self.tolerance)):
            raise ValueError(f"Objective tolerance for {self.metric!r} cannot be negative.")
        if self.sweep.casefold() not in {"alpha", "beta"}:
            raise ValueError(f"Objective sweep must be alpha or beta, got {self.sweep!r}.")
        if self.reducer.casefold() not in {"max", "min", "mean", "average", "absmax", "max_abs", "absmin", "min_abs", "last", "final"}:
            raise ValueError(f"Unsupported metric reducer {self.reducer!r}.")
        if self.label is not None and not self.label.strip():
            raise ValueError("An objective label cannot be blank.")
        for key, requested in self.conditions.items():
            if not str(key).strip():
                raise ValueError("An objective condition name cannot be blank.")
            if isinstance(requested, (int, float)):
                if not math.isfinite(float(requested)):
                    raise ValueError(f"Objective condition {key!r} must be finite.")
                continue
            bounds = list(requested)
            if len(bounds) != 2:
                raise ValueError(f"Objective condition {key!r} must be a number or [minimum, maximum].")
            minimum, maximum = (float(value) for value in bounds)
            if not (math.isfinite(minimum) and math.isfinite(maximum) and minimum <= maximum):
                raise ValueError(f"Objective condition {key!r} must have finite, increasing bounds.")

    @property
    def name(self) -> str:
        return self.label or f"{self.metric}:{self.goal}"

    def cost(self, value: float) -> float:
        scale = self.scale or (max(abs(self.target), 1.0) if self.target is not None else 1.0)
        goal = self.goal.casefold()
        if goal == "maximize":
            return -value / scale
        if goal == "minimize":
            return value / scale
        assert self.target is not None
        if goal == "target":
            return abs(value - self.target) / scale
        if goal == "at_least":
            return max(0.0, self.target - value) / scale
        return max(0.0, value - self.target) / scale

    def satisfied(self, value: float) -> bool:
        if not self.hard:
            return True
        tolerance = self.tolerance or 0.0
        goal = self.goal.casefold()
        if goal == "target":
            assert self.target is not None
            return abs(value - self.target) <= tolerance
        if goal == "at_least":
            assert self.target is not None
            return value >= self.target - tolerance
        if goal == "at_most":
            assert self.target is not None
            return value <= self.target + tolerance
        return True

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ObjectiveSpec":
        allowed = {
            "metric",
            "goal",
            "target",
            "weight",
            "scale",
            "sweep",
            "reducer",
            "conditions",
            "hard",
            "tolerance",
            "label",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"Unknown objective setting(s): {', '.join(sorted(unknown))}.")
        conditions = payload.get("conditions", {}) or {}
        if not isinstance(conditions, Mapping):
            raise ValueError("Objective conditions must be a JSON object.")
        return cls(
            metric=str(payload["metric"]),
            goal=str(payload.get("goal", "maximize")),
            target=None if payload.get("target") is None else float(payload["target"]),
            weight=float(payload.get("weight", 1.0)),
            scale=None if payload.get("scale") is None else float(payload["scale"]),
            sweep=str(payload.get("sweep", "alpha")),
            reducer=str(payload.get("reducer", "max")),
            conditions=conditions,  # type: ignore[arg-type]
            hard=_json_boolean(payload, "hard", False),
            tolerance=None if payload.get("tolerance") is None else float(payload["tolerance"]),
            label=None if payload.get("label") is None else str(payload["label"]),
        )


@dataclass(frozen=True)
class SweepDefinition:
    kind: str
    start: float
    end: float
    increment: float
    speed: float = 70.0
    fixed_angle: float = 0.0
    wait_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.kind.casefold() not in {"alpha", "beta"}:
            raise ValueError("Sweep kind must be alpha or beta.")
        values = (self.start, self.end, self.increment, self.speed, self.fixed_angle, self.wait_seconds)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Sweep values must be finite.")
        if self.end <= self.start:
            raise ValueError("Sweep end must be greater than sweep start.")
        if self.increment <= 0:
            raise ValueError("Sweep increment must be positive.")
        intervals = (self.end - self.start) / self.increment
        if not math.isclose(intervals, round(intervals), rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("Sweep range must be evenly divisible by its increment so the end point is tested.")
        if self.speed <= 0:
            raise ValueError("Sweep speed must be positive.")
        if self.wait_seconds <= 0:
            raise ValueError("Sweep timeout must be positive.")

    def values(self) -> list[float]:
        count = round((self.end - self.start) / self.increment)
        return [self.start + index * self.increment for index in range(count + 1)]

    @classmethod
    def from_dict(cls, kind: str, payload: Mapping[str, object]) -> "SweepDefinition":
        allowed = {"start", "end", "increment", "speed", "fixed_angle", "wait_seconds"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"Unknown {kind}-sweep setting(s): {', '.join(sorted(unknown))}.")
        defaults = {"alpha": (-8.0, 12.0, 1.0), "beta": (-8.0, 8.0, 1.0)}
        start, end, increment = defaults[kind]
        return cls(
            kind,
            float(payload.get("start", start)),
            float(payload.get("end", end)),
            float(payload.get("increment", increment)),
            float(payload.get("speed", 70.0)),
            float(payload.get("fixed_angle", 0.0)),
            float(payload.get("wait_seconds", 60.0)),
        )


@dataclass(frozen=True)
class AnalysisPlan:
    alpha: SweepDefinition = SweepDefinition("alpha", -8.0, 12.0, 1.0)
    beta: SweepDefinition = SweepDefinition("beta", -8.0, 8.0, 1.0)
    run_modal: bool = True
    stability_control_start: float = -1.0
    stability_control_end: float = 1.0
    stability_control_increment: float = 1.0

    def __post_init__(self) -> None:
        if self.alpha.kind.casefold() != "alpha" or self.beta.kind.casefold() != "beta":
            raise ValueError("AnalysisPlan requires an alpha sweep followed by a beta sweep.")
        values = (
            self.stability_control_start,
            self.stability_control_end,
            self.stability_control_increment,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Stability control sequence values must be finite.")
        if self.stability_control_end <= self.stability_control_start or self.stability_control_increment <= 0:
            raise ValueError("Stability control range must be increasing with a positive increment.")
        intervals = (self.stability_control_end - self.stability_control_start) / self.stability_control_increment
        if not math.isclose(intervals, round(intervals), rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                "Stability control range must be evenly divisible by its increment so the end point is tested."
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "AnalysisPlan":
        allowed = {
            "alpha",
            "beta",
            "run_modal",
            "stability_control_start",
            "stability_control_end",
            "stability_control_increment",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"Unknown analysis setting(s): {', '.join(sorted(unknown))}.")
        alpha = payload.get("alpha", {}) or {}
        beta = payload.get("beta", {}) or {}
        if not isinstance(alpha, Mapping) or not isinstance(beta, Mapping):
            raise ValueError("analysis.alpha and analysis.beta must be JSON objects.")
        return cls(
            alpha=SweepDefinition.from_dict("alpha", alpha),
            beta=SweepDefinition.from_dict("beta", beta),
            run_modal=_json_boolean(payload, "run_modal", True),
            stability_control_start=float(payload.get("stability_control_start", -1.0)),
            stability_control_end=float(payload.get("stability_control_end", 1.0)),
            stability_control_increment=float(payload.get("stability_control_increment", 1.0)),
        )


@dataclass
class AnalysisBundle:
    alpha_rows: list[dict[str, float]]
    beta_rows: list[dict[str, float]]
    modal: ModalAnalysis | None
    files: dict[str, str]
    context: MetricContext


class AnalysisBackend(ABC):
    """Interface between deterministic optimization and XFLR/simulation I/O."""

    @abstractmethod
    def evaluate(self, design: Mapping[str, DesignValue], evaluation_id: str) -> AnalysisBundle:
        raise NotImplementedError


@dataclass
class EvaluationRecord:
    evaluation_id: str
    phase: str
    design: Design
    status: str
    objective_values: dict[str, float] = field(default_factory=dict)
    objective_costs: dict[str, float] = field(default_factory=dict)
    loss: float = math.inf
    feasible: bool = False
    stability: StabilityAssessment | None = None
    files: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluation_id": self.evaluation_id,
            "phase": self.phase,
            "design": self.design,
            "status": self.status,
            "objective_values": self.objective_values,
            "objective_costs": self.objective_costs,
            "loss": None if not math.isfinite(self.loss) else self.loss,
            "feasible": self.feasible,
            "stability": None if self.stability is None else self.stability.to_dict(),
            "files": self.files,
            "error": self.error,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class HybridSearchSettings:
    monte_carlo_samples: int = 12
    elite_count: int = 2
    local_iterations: int = 3
    learning_rate: float = 0.2
    gradient_fraction: float = 0.04
    convergence_tolerance: float = 1.0e-4
    random_seed: int = 2026
    failure_penalty: float = 1.0e9
    infeasible_penalty: float = 1.0e5
    max_evaluations: int | None = None

    def __post_init__(self) -> None:
        if self.monte_carlo_samples < 1:
            raise ValueError("monte_carlo_samples must be at least one.")
        if self.elite_count < 1 or self.elite_count > self.monte_carlo_samples:
            raise ValueError("elite_count must be between one and monte_carlo_samples.")
        if self.local_iterations < 0:
            raise ValueError("local_iterations cannot be negative.")
        finite_values = (
            self.learning_rate,
            self.gradient_fraction,
            self.convergence_tolerance,
            self.failure_penalty,
            self.infeasible_penalty,
        )
        if not all(math.isfinite(value) for value in finite_values):
            raise ValueError("Search rates, tolerances, and penalties must be finite.")
        if self.learning_rate <= 0 or self.gradient_fraction <= 0 or self.convergence_tolerance <= 0:
            raise ValueError("learning_rate, gradient_fraction, and convergence_tolerance must be positive.")
        if self.failure_penalty <= 0 or self.infeasible_penalty < 0:
            raise ValueError("failure_penalty must be positive and infeasible_penalty cannot be negative.")
        if self.max_evaluations is not None and self.max_evaluations < self.monte_carlo_samples:
            raise ValueError("max_evaluations cannot be smaller than monte_carlo_samples.")

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "HybridSearchSettings":
        names = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = set(payload) - names
        if unknown:
            raise ValueError(f"Unknown search setting(s): {', '.join(sorted(unknown))}.")
        converted: dict[str, object] = {}
        integer_fields = {"monte_carlo_samples", "elite_count", "local_iterations", "random_seed", "max_evaluations"}
        for name, value in payload.items():
            if name in integer_fields:
                converted[name] = None if value is None else int(value)
            else:
                converted[name] = float(value)
        return cls(**converted)  # type: ignore[arg-type]


@dataclass
class OptimizationRun:
    variables: tuple[DesignVariable, ...]
    objectives: tuple[ObjectiveSpec, ...]
    settings: HybridSearchSettings
    records: list[EvaluationRecord] = field(default_factory=list)
    started_utc: str = ""
    completed_utc: str = ""

    @property
    def successful(self) -> list[EvaluationRecord]:
        return [record for record in self.records if record.status == "ok"]

    @property
    def feasible(self) -> list[EvaluationRecord]:
        return [record for record in self.successful if record.feasible]

    @property
    def best(self) -> EvaluationRecord | None:
        candidates = self.feasible or self.successful
        return min(candidates, key=lambda record: record.loss) if candidates else None

    @property
    def pareto_front(self) -> list[EvaluationRecord]:
        candidates = self.feasible
        frontier: list[EvaluationRecord] = []
        for candidate in candidates:
            costs = tuple(candidate.objective_costs[objective.name] for objective in self.objectives)
            dominated = False
            for other in candidates:
                if other is candidate:
                    continue
                other_costs = tuple(other.objective_costs[objective.name] for objective in self.objectives)
                if all(left <= right for left, right in zip(other_costs, costs)) and any(
                    left < right for left, right in zip(other_costs, costs)
                ):
                    dominated = True
                    break
            if not dominated:
                frontier.append(candidate)
        return sorted(frontier, key=lambda record: record.loss)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "algorithm": "latin-hypercube Monte Carlo exploration + projected finite-difference gradient descent",
            "started_utc": self.started_utc,
            "completed_utc": self.completed_utc,
            "variables": [asdict(variable) for variable in self.variables],
            "objectives": [asdict(objective) for objective in self.objectives],
            "settings": asdict(self.settings),
            "records": [record.to_dict() for record in self.records],
            "best_evaluation_id": None if self.best is None else self.best.evaluation_id,
            "pareto_evaluation_ids": [record.evaluation_id for record in self.pareto_front],
            "counts": {
                "attempted": len(self.records),
                "successful": len(self.successful),
                "feasible": len(self.feasible),
                "failed": len(self.records) - len(self.successful),
            },
        }


class HybridAircraftOptimizer:
    """Explore globally, then refine elite continuous designs locally."""

    def __init__(
        self,
        backend: AnalysisBackend,
        variables: Sequence[DesignVariable],
        objectives: Sequence[ObjectiveSpec],
        output_directory: str | Path,
        *,
        stability_criteria: StabilityCriteria = StabilityCriteria(),
        settings: HybridSearchSettings = HybridSearchSettings(),
        progress: Callable[[EvaluationRecord, OptimizationRun], None] | None = None,
    ) -> None:
        if not variables:
            raise ValueError("At least one design variable is required.")
        if not objectives:
            raise ValueError("At least one objective is required.")
        if len({variable.name for variable in variables}) != len(variables):
            raise ValueError("Design variable names must be unique.")
        if len({objective.name for objective in objectives}) != len(objectives):
            raise ValueError("Objective labels/names must be unique.")
        self.backend = backend
        self.variables = tuple(variables)
        self.objectives = tuple(objectives)
        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self.stability_criteria = stability_criteria
        self.settings = settings
        self.progress = progress
        self._cache: dict[tuple[tuple[str, str], ...], EvaluationRecord] = {}
        self._actual_evaluations = 0

    def _design_key(self, design: Mapping[str, DesignValue]) -> tuple[tuple[str, str], ...]:
        rendered: list[tuple[str, str]] = []
        for variable in self.variables:
            value = variable.project(design[variable.name])
            rendered.append((variable.name, f"{value:.12g}" if isinstance(value, float) else str(value)))
        return tuple(rendered)

    def _project(self, design: Mapping[str, DesignValue]) -> Design:
        return {variable.name: variable.project(design[variable.name]) for variable in self.variables}

    def _checkpoint(self, run: OptimizationRun) -> None:
        destination = self.output_directory / "optimization_checkpoint.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(run.to_dict(), indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(destination)

    def _evaluate(self, design: Mapping[str, DesignValue], phase: str, run: OptimizationRun) -> EvaluationRecord:
        projected = self._project(design)
        key = self._design_key(projected)
        if key in self._cache:
            return self._cache[key]
        if self.settings.max_evaluations is not None and self._actual_evaluations >= self.settings.max_evaluations:
            raise StopIteration("Configured max_evaluations reached.")
        self._actual_evaluations += 1
        evaluation_id = f"eval_{self._actual_evaluations:04d}"
        started = time.monotonic()
        try:
            bundle = self.backend.evaluate(projected, evaluation_id)
            stability = assess_stability(bundle.alpha_rows, bundle.beta_rows, bundle.modal, self.stability_criteria)
            objective_values: dict[str, float] = {}
            objective_costs: dict[str, float] = {}
            hard_ok = True
            for objective in self.objectives:
                values = metric_values(
                    objective.metric,
                    alpha_rows=bundle.alpha_rows,
                    beta_rows=bundle.beta_rows,
                    modal=bundle.modal,
                    sweep=objective.sweep,
                    context=bundle.context,
                    conditions=objective.conditions,
                )
                value = reduce_metric(values, objective.reducer)
                objective_values[objective.name] = value
                objective_costs[objective.name] = objective.cost(value)
                hard_ok = hard_ok and objective.satisfied(value)
            feasible = stability.feasible and hard_ok
            loss = sum(
                objective.weight * objective_costs[objective.name] for objective in self.objectives
            )
            if not feasible:
                loss += self.settings.infeasible_penalty
            record = EvaluationRecord(
                evaluation_id,
                phase,
                projected,
                "ok",
                objective_values,
                objective_costs,
                loss,
                feasible,
                stability,
                bundle.files,
                elapsed_seconds=time.monotonic() - started,
            )
        except Exception as error:
            record = EvaluationRecord(
                evaluation_id,
                phase,
                projected,
                "failed",
                loss=self.settings.failure_penalty,
                error=f"{type(error).__name__}: {error}",
                elapsed_seconds=time.monotonic() - started,
            )
        self._cache[key] = record
        run.records.append(record)
        self._checkpoint(run)
        if self.progress is not None:
            self.progress(record, run)
        return record

    def _monte_carlo_designs(self) -> list[Design]:
        """Generate a reproducible Latin-hypercube Monte Carlo distribution."""

        count = self.settings.monte_carlo_samples
        rng = random.Random(self.settings.random_seed)
        columns: dict[str, list[DesignValue]] = {}
        for variable in self.variables:
            if variable.categorical:
                values = [variable.choices[index % len(variable.choices)] for index in range(count)]
                rng.shuffle(values)
                columns[variable.name] = values
                continue
            assert variable.minimum is not None
            bins = list(range(count))
            rng.shuffle(bins)
            columns[variable.name] = [
                variable.project(variable.minimum + ((bin_index + rng.random()) / count) * variable.range)
                for bin_index in bins
            ]
        designs = [
            {variable.name: columns[variable.name][index] for variable in self.variables}
            for index in range(count)
        ]
        baseline = {variable.name: variable.default() for variable in self.variables}
        designs[0] = self._project(baseline)
        return designs

    def _gradient_refine(self, seed: EvaluationRecord, seed_index: int, run: OptimizationRun) -> None:
        numeric = [variable for variable in self.variables if not variable.categorical]
        if not numeric or self.settings.local_iterations == 0:
            return
        current = seed
        learning_rate = self.settings.learning_rate
        for _ in range(self.settings.local_iterations):
            gradients: dict[str, float] = {}
            try:
                for variable in numeric:
                    assert variable.minimum is not None and variable.maximum is not None
                    current_value = float(current.design[variable.name])
                    delta = max(
                        variable.range * self.settings.gradient_fraction,
                        variable.step or 0.0,
                    )
                    plus = dict(current.design)
                    minus = dict(current.design)
                    plus[variable.name] = variable.project(current_value + delta)
                    minus[variable.name] = variable.project(current_value - delta)
                    plus_record = self._evaluate(plus, f"gradient_{seed_index + 1}", run)
                    minus_record = self._evaluate(minus, f"gradient_{seed_index + 1}", run)
                    denominator = float(plus[variable.name]) - float(minus[variable.name])
                    gradients[variable.name] = (
                        (plus_record.loss - minus_record.loss) / denominator if denominator else 0.0
                    )
            except StopIteration:
                return
            normalized = {
                variable.name: gradients[variable.name] * variable.range for variable in numeric
            }
            norm = math.sqrt(sum(value * value for value in normalized.values()))
            if not math.isfinite(norm) or norm <= self.settings.convergence_tolerance:
                return
            trial = dict(current.design)
            for variable in numeric:
                direction = normalized[variable.name] / norm
                normalized_current = (float(current.design[variable.name]) - float(variable.minimum)) / variable.range
                normalized_trial = normalized_current - learning_rate * direction
                trial[variable.name] = variable.project(float(variable.minimum) + normalized_trial * variable.range)
            try:
                trial_record = self._evaluate(trial, f"descent_{seed_index + 1}", run)
            except StopIteration:
                return
            improvement = current.loss - trial_record.loss
            if improvement > self.settings.convergence_tolerance:
                current = trial_record
                learning_rate = min(0.75, learning_rate * 1.15)
            else:
                learning_rate *= 0.5
                if learning_rate < self.settings.convergence_tolerance:
                    return

    def optimize(self) -> OptimizationRun:
        from datetime import datetime, timezone

        run = OptimizationRun(self.variables, self.objectives, self.settings)
        run.started_utc = datetime.now(timezone.utc).isoformat()
        for design in self._monte_carlo_designs():
            try:
                self._evaluate(design, "monte_carlo", run)
            except StopIteration:
                break
        ranked = sorted(run.successful, key=lambda record: record.loss)
        for seed_index, seed in enumerate(ranked[: self.settings.elite_count]):
            self._gradient_refine(seed, seed_index, run)
        run.completed_utc = datetime.now(timezone.utc).isoformat()
        self._checkpoint(run)
        return run


class GuiXFLRAnalysisBackend(AnalysisBackend):
    """Evaluate a design by editing XML and driving an XFLR5 GUI adapter."""

    _AIRFOIL_KEYS = {"airfoil", "left_airfoil", "right_airfoil"}

    def __init__(
        self,
        gui: Any,
        plane: Any,
        baseline_component: str | Path,
        output_directory: str | Path,
        analysis: AnalysisPlan,
        *,
        part: str = "wing",
        sections: tuple[int, int | None] = (1, None),
        allowed_airfoils: Sequence[str] = (),
        available_airfoils: Sequence[str] = (),
        span_limit: float | None = None,
        stability_required: bool = True,
        clear_polars_between_designs: bool = True,
        air_density: float = 1.225,
        reference_area_m2: float | None = None,
    ) -> None:
        self.gui = gui
        self.plane = plane
        self.baseline_component = Path(baseline_component)
        if not self.baseline_component.is_file():
            raise FileNotFoundError(self.baseline_component)
        self.output_directory = Path(output_directory)
        self.raw_directory = self.output_directory / "raw"
        self.design_directory = self.output_directory / "designs"
        self.raw_directory.mkdir(parents=True, exist_ok=True)
        self.design_directory.mkdir(parents=True, exist_ok=True)
        self.analysis = analysis
        self.part = part
        self.sections = sections
        self.allowed_airfoils = set(allowed_airfoils)
        self.available_airfoils = set(available_airfoils)
        self.span_limit = span_limit
        self.stability_required = stability_required
        self.clear_polars_between_designs = clear_polars_between_designs
        self.air_density = air_density
        self.reference_area_m2 = reference_area_m2
        self.native_scale = PlaneXmlEditor.length_unit_to_meter(self.baseline_component)

    def _airfoils(self, design: Mapping[str, DesignValue]) -> tuple[str | None, str | None]:
        common = design.get("airfoil")
        left = design.get("left_airfoil", common)
        right = design.get("right_airfoil", left)
        return (None if left is None else str(left), None if right is None else str(right))

    def _validate_airfoils(self, left: str | None, right: str | None) -> None:
        selected = {value for value in (left, right) if value}
        if self.allowed_airfoils and not selected.issubset(self.allowed_airfoils):
            raise ConstraintError(
                f"Design uses airfoil(s) outside the allowed set: {', '.join(sorted(selected - self.allowed_airfoils))}."
            )
        if self.available_airfoils and not selected.issubset(self.available_airfoils):
            raise ConstraintError(
                f"No .dat file was found for: {', '.join(sorted(selected - self.available_airfoils))}."
            )

    def evaluate(self, design: Mapping[str, DesignValue], evaluation_id: str) -> AnalysisBundle:
        design_path = self.design_directory / f"{evaluation_id}.xml"
        alpha_path = self.raw_directory / f"{evaluation_id}_alpha.csv"
        beta_path = self.raw_directory / f"{evaluation_id}_beta.csv"
        modal_path = self.raw_directory / f"{evaluation_id}_modal.txt"
        for generated_path in (alpha_path, beta_path, modal_path):
            generated_path.unlink(missing_ok=True)
        shutil.copy2(self.baseline_component, design_path)
        left, right = self._airfoils(design)
        self._validate_airfoils(left, right)
        if left is not None:
            PlaneXmlEditor.replace_airfoils(design_path, left, right, self.sections)
        geometry = {key: float(value) for key, value in design.items() if key not in self._AIRFOIL_KEYS}
        PlaneXmlEditor.apply_geometry(design_path, geometry)
        if self.span_limit is not None and PlaneXmlEditor.span(design_path) > self.span_limit:
            raise ConstraintError(
                f"Realized span {PlaneXmlEditor.span(design_path):g} exceeds limit {self.span_limit:g}."
            )
        self.gui.replace_part_from_xml(self.plane, design_path, self.part)
        files = {"design_xml": str(design_path)}
        try:
            for definition, destination in (
                (self.analysis.alpha, alpha_path),
                (self.analysis.beta, beta_path),
            ):
                self.gui.alpha_or_beta_sweep(
                    self.plane,
                    f"{evaluation_id}_{definition.kind}",
                    destination,
                    speed=definition.speed,
                    sweep_start=definition.start,
                    sweep_end=definition.end,
                    sweep_increment=definition.increment,
                    fixed_angle=definition.fixed_angle,
                    wait_seconds=definition.wait_seconds,
                    sweep_kind=definition.kind,
                )
                files[f"{definition.kind}_results"] = str(destination)
            alpha_rows = parse_result_table(alpha_path)
            beta_rows = parse_result_table(beta_path)
            validate_sweep_coverage(
                alpha_rows,
                "alpha",
                self.analysis.alpha.values(),
                tolerance=max(1.0e-6, self.analysis.alpha.increment * 0.05),
            )
            validate_sweep_coverage(
                beta_rows,
                "beta",
                self.analysis.beta.values(),
                tolerance=max(1.0e-6, self.analysis.beta.increment * 0.05),
            )
            modal: ModalAnalysis | None = None
            if self.analysis.run_modal:
                runner = getattr(self.gui, "stability_analysis", None)
                if not callable(runner):
                    if self.stability_required:
                        raise RuntimeError("The selected GUI backend cannot export XFLR modal operating-point data.")
                else:
                    runner(
                        self.plane,
                        f"{evaluation_id}_stability",
                        modal_path,
                        control_start=self.analysis.stability_control_start,
                        control_end=self.analysis.stability_control_end,
                        control_increment=self.analysis.stability_control_increment,
                        wait_seconds=max(self.analysis.alpha.wait_seconds, self.analysis.beta.wait_seconds),
                    )
                    files["modal_results"] = str(modal_path)
                    modal = parse_modal_report(modal_path)
            area_m2 = self.reference_area_m2
            if area_m2 is None:
                area_m2 = PlaneXmlEditor.planform_area(design_path) * self.native_scale**2
            context = MetricContext(self.air_density, area_m2, self.analysis.alpha.speed)
            return AnalysisBundle(alpha_rows, beta_rows, modal, files, context)
        finally:
            if self.clear_polars_between_designs:
                self.gui.delete_polars(self.plane)


class AnalyticDemoBackend(AnalysisBackend):
    """Deterministic stand-in used by tests and the zero-setup demo package."""

    def __init__(self, output_directory: str | Path, analysis: AnalysisPlan | None = None) -> None:
        self.output_directory = Path(output_directory)
        self.raw_directory = self.output_directory / "raw"
        self.raw_directory.mkdir(parents=True, exist_ok=True)
        self.analysis = analysis or AnalysisPlan()

    @staticmethod
    def _numeric(design: Mapping[str, DesignValue], name: str, default: float) -> float:
        return float(design.get(name, default))

    @staticmethod
    def _airfoil_factor(design: Mapping[str, DesignValue]) -> float:
        name = str(design.get("airfoil", design.get("left_airfoil", "N22")))
        digest = hashlib.sha256(name.encode("utf-8")).digest()
        return (digest[0] / 255.0 - 0.5) * 0.08

    @staticmethod
    def _write_table(path: Path, rows: Sequence[Mapping[str, float]]) -> None:
        headers = ["alpha", "beta", "CL", "CDi", "CDv", "CD", "CY", "Cl", "Cm", "Cn", "Cni", "QInf", "XCP"]
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _write_modal(path: Path, longitudinal: Sequence[complex], lateral: Sequence[complex]) -> None:
        def line(values: Sequence[complex]) -> str:
            return "   |   ".join(f"{value.real:.8g}+{value.imag:.8g}i" for value in values)

        path.write_text(
            "XFLR5 analytic test fixture\n\n"
            "   ___Longitudinal modes____\n\n"
            f"      Eigenvalue:  {line(longitudinal)}\n\n"
            "   ___Lateral modes____\n\n"
            f"      Eigenvalue:  {line(lateral)}\n",
            encoding="utf-8",
        )

    def evaluate(self, design: Mapping[str, DesignValue], evaluation_id: str) -> AnalysisBundle:
        span = self._numeric(design, "span", 60.0)
        chord_scale = self._numeric(design, "chord_scale", 1.0)
        sweep = self._numeric(design, "sweep_deg", 18.0)
        twist = self._numeric(design, "tip_twist_deg", -2.0)
        dihedral = self._numeric(design, "dihedral_deg", 4.0)
        foil = self._airfoil_factor(design)
        span_factor = span / 60.0
        efficiency = math.exp(-((sweep - 16.0) / 18.0) ** 2 - ((twist + 2.5) / 5.0) ** 2)
        lift_slope_degree = (0.075 + 0.025 * efficiency + foil) * span_factor * math.sqrt(chord_scale)
        cm_slope_degree = -0.012 - 0.00022 * (span - 48.0) - 0.0003 * dihedral
        cm_zero = 0.025 + 0.003 * (twist + 2.0)
        qinf = self.analysis.alpha.speed

        alpha_rows: list[dict[str, float]] = []
        for alpha in self.analysis.alpha.values():
            cl = 0.28 + lift_slope_degree * alpha
            cdi = cl * cl / (math.pi * max(2.5, 7.2 * span_factor / chord_scale) * (0.72 + 0.18 * efficiency))
            cdv = 0.016 + 0.000025 * (sweep - 14.0) ** 2 + 0.002 * (chord_scale - 0.92) ** 2
            cd = cdi + cdv
            alpha_rows.append(
                {
                    "alpha": alpha,
                    "beta": self.analysis.alpha.fixed_angle,
                    "CL": cl,
                    "CDi": cdi,
                    "CDv": cdv,
                    "CD": cd,
                    "CY": 0.0,
                    "Cl": 0.0,
                    "Cm": cm_zero + cm_slope_degree * alpha,
                    "Cn": 0.0,
                    "Cni": 0.0,
                    "QInf": qinf,
                    "XCP": 0.25 + 0.01 * alpha,
                }
            )

        cn_slope_degree = 0.0028 + 0.00008 * (span - 45.0) + 0.00013 * max(sweep, 0.0)
        roll_slope_degree = -0.0018 - 0.00025 * max(dihedral, 0.0)
        cy_slope_degree = -0.006 - 0.0001 * span
        beta_rows: list[dict[str, float]] = []
        for beta in self.analysis.beta.values():
            beta_rows.append(
                {
                    "alpha": self.analysis.beta.fixed_angle,
                    "beta": beta,
                    "CL": 0.28 - 0.0008 * beta * beta,
                    "CDi": 0.006 + 0.00015 * beta * beta,
                    "CDv": 0.016,
                    "CD": 0.022 + 0.00015 * beta * beta,
                    "CY": cy_slope_degree * beta,
                    "Cl": roll_slope_degree * beta,
                    "Cm": cm_zero,
                    "Cn": cn_slope_degree * beta,
                    "Cni": cn_slope_degree * beta * 0.8,
                    "QInf": self.analysis.beta.speed,
                    "XCP": 0.25,
                }
            )

        # The local optimum is stable; extreme geometry creates slow modes so
        # the gate and pilot-correctability logic are exercised by default.
        phugoid_real = -0.025 + max(0.0, 48.0 - span) * 0.003
        short_real = -1.8 + 0.015 * abs(sweep - 16.0)
        spiral_real = -0.035 + max(0.0, 2.5 - dihedral) * 0.018
        roll_real = -2.6 + 0.01 * abs(sweep)
        longitudinal = (
            complex(phugoid_real, 0.55),
            complex(phugoid_real, -0.55),
            complex(short_real, 6.2),
            complex(short_real, -6.2),
        )
        lateral = (
            complex(-0.32, 2.4),
            complex(-0.32, -2.4),
            complex(roll_real, 0.0),
            complex(spiral_real, 0.0),
        )
        alpha_path = self.raw_directory / f"{evaluation_id}_alpha.csv"
        beta_path = self.raw_directory / f"{evaluation_id}_beta.csv"
        modal_path = self.raw_directory / f"{evaluation_id}_modal.txt"
        self._write_table(alpha_path, alpha_rows)
        self._write_table(beta_path, beta_rows)
        files = {
            "alpha_results": str(alpha_path),
            "beta_results": str(beta_path),
        }
        modal = None
        if self.analysis.run_modal:
            self._write_modal(modal_path, longitudinal, lateral)
            modal = parse_modal_report(modal_path)
            files["modal_results"] = str(modal_path)
        area = 0.65 * span_factor * chord_scale
        return AnalysisBundle(
            alpha_rows,
            beta_rows,
            modal,
            files,
            MetricContext(1.225, area, qinf),
        )


def console_progress(record: EvaluationRecord, run: OptimizationRun) -> None:
    best = run.best
    best_text = "n/a" if best is None else f"{best.loss:.6g} ({best.evaluation_id})"
    result = "PASS" if record.feasible else ("FAIL" if record.status == "ok" else "ERROR")
    print(
        f"[{len(run.records):04d}] {record.phase:<14} {record.evaluation_id} {result:<5} "
        f"loss={record.loss:.6g} best={best_text}",
        flush=True,
    )


__all__ = [
    "AnalysisBackend",
    "AnalysisBundle",
    "AnalysisPlan",
    "AnalyticDemoBackend",
    "Design",
    "DesignVariable",
    "EvaluationRecord",
    "GuiXFLRAnalysisBackend",
    "HybridAircraftOptimizer",
    "HybridSearchSettings",
    "ObjectiveSpec",
    "OptimizationRun",
    "SweepDefinition",
    "console_progress",
]
