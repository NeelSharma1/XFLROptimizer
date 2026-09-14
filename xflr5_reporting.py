"""Create the complete machine-readable and visual optimization package."""

from __future__ import annotations

import csv
import html
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from xflr5_optimization import EvaluationRecord, OptimizationRun
from xflr5_results import parse_modal_report, parse_result_table


_COLORS = {"monte_carlo": "#4C78A8", "gradient_1": "#F58518", "descent_1": "#E45756"}


def _color_for_phase(phase: str) -> str:
    if phase.startswith("gradient"):
        return "#F58518"
    if phase.startswith("descent"):
        return "#E45756"
    return _COLORS.get(phase, "#4C78A8")


def _save_figure(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def _empty_figure(title: str, message: str, path: Path) -> None:
    fig, axis = plt.subplots(figsize=(8, 4.5))
    axis.set_title(title)
    axis.text(0.5, 0.5, message, ha="center", va="center", transform=axis.transAxes)
    axis.set_axis_off()
    _save_figure(fig, path)


def _convergence_plot(run: OptimizationRun, path: Path) -> None:
    successful = [(index, record) for index, record in enumerate(run.records, 1) if record.status == "ok"]
    if not successful:
        _empty_figure("Optimization convergence", "No successful evaluations", path)
        return
    indices = [index for index, _ in successful]
    losses = [record.loss for _, record in successful]
    cumulative: list[float] = []
    best = math.inf
    for loss in losses:
        best = min(best, loss)
        cumulative.append(best)
    fig, axis = plt.subplots(figsize=(9, 5))
    axis.plot(indices, cumulative, color="#222222", linewidth=2.2, label="best loss so far")
    for phase in sorted({record.phase for _, record in successful}):
        phase_points = [(index, record.loss) for index, record in successful if record.phase == phase]
        axis.scatter(
            [point[0] for point in phase_points],
            [point[1] for point in phase_points],
            s=28,
            alpha=0.7,
            color=_color_for_phase(phase),
            label=phase,
        )
    axis.set_xlabel("Actual design evaluation")
    axis.set_ylabel("Penalized weighted loss (lower is better)")
    axis.set_title("Monte Carlo exploration and gradient refinement")
    axis.grid(alpha=0.25)
    axis.legend(fontsize=8)
    _save_figure(fig, path)


def _objective_history_plot(run: OptimizationRun, path: Path) -> None:
    if not run.objectives or not run.successful:
        _empty_figure("Objective history", "No objective data", path)
        return
    fig, axes = plt.subplots(len(run.objectives), 1, figsize=(9, max(3.2, 2.8 * len(run.objectives))), squeeze=False)
    for axis, objective in zip(axes[:, 0], run.objectives):
        points = [
            (index, record.objective_values[objective.name], record.feasible, record.phase)
            for index, record in enumerate(run.records, 1)
            if record.status == "ok" and objective.name in record.objective_values
        ]
        axis.scatter(
            [point[0] for point in points],
            [point[1] for point in points],
            c=["#54A24B" if point[2] else _color_for_phase(point[3]) for point in points],
            s=28,
            alpha=0.8,
        )
        if objective.target is not None:
            axis.axhline(objective.target, color="#B279A2", linestyle="--", linewidth=1.5, label="target/limit")
            axis.legend(fontsize=8)
        axis.set_ylabel(objective.metric)
        axis.set_title(f"{objective.name} ({objective.reducer} over {objective.sweep} sweep)", fontsize=10)
        axis.grid(alpha=0.2)
    axes[-1, 0].set_xlabel("Actual design evaluation")
    _save_figure(fig, path)


def _frontier_plot(run: OptimizationRun, path: Path) -> None:
    records = run.feasible
    if not records:
        _empty_figure("Design frontier", "No stability-feasible designs", path)
        return
    frontier_ids = {record.evaluation_id for record in run.pareto_front}
    if len(run.objectives) >= 2:
        first, second = run.objectives[:2]
        x = [record.objective_values[first.name] for record in records]
        y = [record.objective_values[second.name] for record in records]
        colors = [record.loss for record in records]
        fig, (axis, parallel) = plt.subplots(2, 1, figsize=(10, 11))
        points = axis.scatter(x, y, c=colors, cmap="viridis_r", s=42, alpha=0.8, label="feasible design")
        frontier = [record for record in records if record.evaluation_id in frontier_ids]
        axis.scatter(
            [record.objective_values[first.name] for record in frontier],
            [record.objective_values[second.name] for record in frontier],
            marker="*",
            s=150,
            facecolor="#E45756",
            edgecolor="black",
            linewidth=0.5,
            label="Pareto frontier",
        )
        axis.set_xlabel(first.name)
        axis.set_ylabel(second.name)
        axis.set_title("Feasible multi-objective design frontier")
        axis.grid(alpha=0.22)
        axis.legend()
        fig.colorbar(points, ax=axis, label="Weighted loss")

        objective_names = [objective.name for objective in run.objectives]
        columns = [
            [record.objective_values[name] for record in records]
            for name in objective_names
        ]
        minima = [min(column) for column in columns]
        maxima = [max(column) for column in columns]
        for record in records:
            normalized = [
                0.5 if maximum == minimum else (record.objective_values[name] - minimum) / (maximum - minimum)
                for name, minimum, maximum in zip(objective_names, minima, maxima)
            ]
            if record is run.best:
                style = {"color": "#54A24B", "linewidth": 2.8, "alpha": 1.0, "label": "selected design"}
            elif record.evaluation_id in frontier_ids:
                style = {"color": "#E45756", "linewidth": 1.5, "alpha": 0.65, "label": "Pareto design"}
            else:
                style = {"color": "#8D99A6", "linewidth": 0.8, "alpha": 0.22, "label": "feasible design"}
            parallel.plot(range(len(objective_names)), normalized, **style)
        handles, labels = parallel.get_legend_handles_labels()
        unique = dict(zip(labels, handles))
        parallel.legend(unique.values(), unique.keys(), fontsize=8, loc="best")
        parallel.set_xticks(range(len(objective_names)), objective_names, rotation=25, ha="right")
        parallel.set_ylabel("Normalized observed value (0=min, 1=max)")
        parallel.set_title("All configured objective dimensions")
        parallel.set_ylim(-0.05, 1.05)
        parallel.grid(alpha=0.22)
        _save_figure(fig, path)
        return
    objective = run.objectives[0]
    fig, axis = plt.subplots(figsize=(8, 5))
    axis.scatter(
        range(1, len(records) + 1),
        [record.objective_values[objective.name] for record in records],
        c=[record.loss for record in records],
        cmap="viridis_r",
        s=45,
    )
    axis.set_xlabel("Feasible design")
    axis.set_ylabel(objective.name)
    axis.set_title("Feasible objective frontier")
    axis.grid(alpha=0.22)
    _save_figure(fig, path)


def _design_variables_plot(run: OptimizationRun, path: Path) -> None:
    if not run.records:
        _empty_figure("Design variables", "No designs evaluated", path)
        return
    fig, axes = plt.subplots(len(run.variables), 1, figsize=(9, max(3.2, 2.4 * len(run.variables))), squeeze=False)
    for axis, variable in zip(axes[:, 0], run.variables):
        records = [(index, record) for index, record in enumerate(run.records, 1) if record.status == "ok"]
        if variable.categorical:
            mapping = {choice: index for index, choice in enumerate(variable.choices)}
            values = [mapping[str(record.design[variable.name])] for _, record in records]
            axis.set_yticks(list(mapping.values()), list(mapping.keys()))
        else:
            values = [float(record.design[variable.name]) for _, record in records]
            assert variable.minimum is not None and variable.maximum is not None
            axis.axhspan(variable.minimum, variable.maximum, color="#4C78A8", alpha=0.06)
        axis.scatter(
            [index for index, _ in records],
            values,
            c=["#54A24B" if record.feasible else _color_for_phase(record.phase) for _, record in records],
            s=25,
        )
        axis.set_ylabel(variable.name)
        axis.grid(alpha=0.18)
    axes[-1, 0].set_xlabel("Actual design evaluation")
    fig.suptitle("Geometry and airfoil designs tested", y=1.002)
    _save_figure(fig, path)


def _comparison_records(run: OptimizationRun) -> tuple[EvaluationRecord | None, EvaluationRecord | None]:
    initial = next((record for record in run.records if record.status == "ok"), None)
    return initial, run.best


def _sweep_plot(run: OptimizationRun, kind: str, path: Path) -> None:
    initial, best = _comparison_records(run)
    if initial is None or best is None:
        _empty_figure(f"{kind.title()} sweep", "Initial or best result is unavailable", path)
        return
    file_key = f"{kind}_results"
    try:
        initial_rows = parse_result_table(initial.files[file_key])
        best_rows = parse_result_table(best.files[file_key])
    except (KeyError, OSError, ValueError) as error:
        _empty_figure(f"{kind.title()} sweep", f"Could not load sweep data: {error}", path)
        return
    if kind == "alpha":
        independent, metrics = "alpha", ("CL", "CD", "Cm")
    else:
        independent, metrics = "beta", ("CY", "Cl", "Cn")
    fig, axes = plt.subplots(len(metrics), 1, figsize=(8, 8), sharex=True)
    for axis, metric in zip(axes, metrics):
        for rows, label, style in (
            (initial_rows, f"initial ({initial.evaluation_id})", "--"),
            (best_rows, f"best ({best.evaluation_id})", "-"),
        ):
            axis.plot(
                [row[independent] for row in rows],
                [row[metric] for row in rows],
                style,
                linewidth=1.8,
                label=label,
            )
        axis.set_ylabel(metric)
        axis.grid(alpha=0.22)
    axes[0].legend(fontsize=8)
    axes[-1].set_xlabel(f"{kind} [deg]")
    fig.suptitle(f"Initial versus selected design: {kind} sweep")
    _save_figure(fig, path)


def _root_locus_plot(run: OptimizationRun, path: Path) -> None:
    best = run.best
    if best is None:
        _empty_figure("Stability root locus", "No selected design", path)
        return
    try:
        modal = parse_modal_report(best.files["modal_results"])
    except (KeyError, OSError, ValueError) as error:
        _empty_figure("Stability root locus", f"Could not load modal data: {error}", path)
        return
    fig, (progress, axis) = plt.subplots(2, 1, figsize=(9, 10))
    modal_history = [
        (index, record.stability.metrics["MAX_MODE_REAL"], record.feasible)
        for index, record in enumerate(run.records, 1)
        if record.status == "ok"
        and record.stability is not None
        and "MAX_MODE_REAL" in record.stability.metrics
    ]
    progress.scatter(
        [point[0] for point in modal_history],
        [point[1] for point in modal_history],
        c=["#54A24B" if point[2] else "#E45756" for point in modal_history],
        s=30,
        alpha=0.75,
    )
    progress.axhline(0.0, color="black", linewidth=1.2)
    progress.set_xlabel("Actual design evaluation")
    progress.set_ylabel("Maximum eigenvalue real part [1/s]")
    progress.set_title("Dynamic-stability progression across tested designs")
    progress.grid(alpha=0.22)
    markers = {"longitudinal": "o", "lateral": "s"}
    colors = {
        "phugoid": "#4C78A8",
        "short_period": "#F58518",
        "dutch_roll": "#54A24B",
        "roll": "#B279A2",
        "spiral": "#E45756",
        "lateral": "#72B7B2",
    }
    labeled: set[str] = set()
    for mode in modal.modes:
        label = mode.name if mode.name not in labeled else None
        labeled.add(mode.name)
        axis.scatter(
            mode.real,
            mode.imaginary / (2.0 * math.pi),
            marker=markers[mode.direction],
            color=colors.get(mode.name, "#555555"),
            s=70,
            label=label,
        )
    axis.axvline(0.0, color="black", linewidth=1.2)
    axis.axvspan(axis.get_xlim()[0], 0.0, color="#54A24B", alpha=0.07)
    axis.set_xlabel("Eigenvalue real part [1/s]")
    axis.set_ylabel("Imaginary part / 2π [Hz]")
    axis.set_title(f"XFLR modal root locus: {best.evaluation_id}")
    axis.grid(alpha=0.22)
    axis.legend(fontsize=8, ncol=2)
    _save_figure(fig, path)


def _write_history(run: OptimizationRun, path: Path) -> None:
    variable_names = [variable.name for variable in run.variables]
    objective_names = [objective.name for objective in run.objectives]
    stability_names = sorted(
        {
            name
            for record in run.records
            if record.stability is not None
            for name in record.stability.metrics
        }
    )
    fields = [
        "evaluation_id",
        "phase",
        "status",
        "feasible",
        "loss",
        "elapsed_seconds",
        *variable_names,
        *objective_names,
        *[f"stability.{name}" for name in stability_names],
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in run.records:
            writer.writerow(
                {
                    "evaluation_id": record.evaluation_id,
                    "phase": record.phase,
                    "status": record.status,
                    "feasible": record.feasible,
                    "loss": "" if not math.isfinite(record.loss) else record.loss,
                    "elapsed_seconds": record.elapsed_seconds,
                    **record.design,
                    **record.objective_values,
                    **(
                        {}
                        if record.stability is None
                        else {f"stability.{name}": value for name, value in record.stability.metrics.items()}
                    ),
                    "error": record.error or "",
                }
            )


def _write_frontier(run: OptimizationRun, path: Path) -> None:
    fields = ["evaluation_id", "loss", *[variable.name for variable in run.variables], *[o.name for o in run.objectives]]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in run.pareto_front:
            writer.writerow(
                {"evaluation_id": record.evaluation_id, "loss": record.loss, **record.design, **record.objective_values}
            )


def _html_report(
    run: OptimizationRun,
    plots: Mapping[str, Path],
    status: str,
    optimization_status: str,
    test_summary: Mapping[str, object] | None,
) -> str:
    best = run.best
    counts = run.to_dict()["counts"]
    best_rows = "<p>No design completed successfully.</p>"
    stability_rows = ""
    if best is not None:
        design = "".join(
            f"<tr><th>{html.escape(name)}</th><td>{html.escape(f'{value:.8g}' if isinstance(value, float) else str(value))}</td></tr>"
            for name, value in best.design.items()
        )
        objectives = "".join(
            f"<tr><th>{html.escape(name)}</th><td>{value:.8g}</td></tr>"
            for name, value in best.objective_values.items()
        )
        best_rows = (
            f"<p>Selected evaluation: <code>{best.evaluation_id}</code>; weighted loss {best.loss:.8g}.</p>"
            f"<div class='tables'><table><caption>Design</caption>{design}</table>"
            f"<table><caption>Objectives</caption>{objectives}</table></div>"
        )
        if best.stability is not None:
            assessment = best.stability
            checks = {
                "Static longitudinal": assessment.static_longitudinal,
                "Static directional": assessment.static_directional,
                "Static lateral": assessment.static_lateral,
                "Positive-lift trim": assessment.trimmed_positive_lift,
                f"Eight modal roots available (required: {'yes' if assessment.modal_required else 'no'})": assessment.modal_complete,
                "Natural modes dynamically stable": assessment.dynamic_stable,
                "Required pilot control axes declared": assessment.control_axes_available,
                "Pilot-correctable screen": assessment.pilot_controllable,
                "Overall stability gate": assessment.feasible,
            }
            stability_rows = "<table><caption>Stability and control screening</caption>" + "".join(
                f"<tr><th>{html.escape(name)}</th><td class='{('pass' if value else 'fail')}'>{'PASS' if value else 'FAIL'}</td></tr>"
                for name, value in checks.items()
            ) + "</table>"
            if assessment.metrics:
                stability_rows += "<table><caption>Extracted stability values</caption>" + "".join(
                    f"<tr><th>{html.escape(name)}</th><td>{value:.8g}</td></tr>"
                    for name, value in assessment.metrics.items()
                ) + "</table>"
            if assessment.reasons:
                stability_rows += "<ul>" + "".join(f"<li>{html.escape(reason)}</li>" for reason in assessment.reasons) + "</ul>"
    images = "".join(
        f"<figure><img src='{html.escape(path.as_posix())}' alt='{html.escape(title)}'><figcaption>{html.escape(title)}</figcaption></figure>"
        for title, path in plots.items()
    )
    if test_summary:
        test_status = html.escape(str(test_summary.get("status", "UNKNOWN")))
        test_rows = "".join(
            f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(test_summary.get(key, '—')))}</td></tr>"
            for label, key in (
                ("Run", "tests_run"),
                ("Passed", "passed"),
                ("Failed", "failed"),
                ("Errors", "errors"),
                ("Skipped", "skipped"),
            )
        )
        tests_html = (
            f"<p>Automated test status: <strong>{test_status}</strong>. "
            "See <code>tests/TEST_RESULTS.txt</code> for the named checks and any traceback.</p>"
            f"<table><caption>Automated test summary</caption>{test_rows}</table>"
        )
    else:
        tests_html = "<p>No test-results artifact was attached to this optimization run.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XFLR5 optimization report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;max-width:1120px;margin:2rem auto;padding:0 1rem;color:#222}}
h1,h2{{color:#18324a}} .status{{font-size:1.25rem;font-weight:700}} .pass{{color:#188038}} .fail{{color:#b3261e}}
table{{border-collapse:collapse;min-width:300px;margin:0 1rem 1rem 0}} th,td{{border:1px solid #ccd6df;padding:.4rem .65rem;text-align:left}}
caption{{font-weight:700;text-align:left;margin:.35rem 0}} .tables{{display:flex;flex-wrap:wrap}} figure{{margin:1.5rem 0}}
img{{max-width:100%;height:auto;border:1px solid #d8e0e7}} figcaption{{font-weight:600;margin-top:.35rem}}
code{{background:#eef2f5;padding:.12rem .3rem}} footer{{color:#5f6b76;font-size:.9rem;margin:2rem 0}}
</style></head><body>
<h1>XFLR5 aircraft optimization report</h1>
<p class="status {'pass' if status == 'PASS' else 'fail'}">Run status: {status}</p>
<p>Optimization status: <strong>{optimization_status}</strong>. The overall status also includes the attached automated test result, when present.</p>
<p>Algorithm: Latin-hypercube Monte Carlo exploration followed by projected finite-difference gradient descent.</p>
<p>Attempted {counts['attempted']} unique designs; {counts['successful']} produced complete data; {counts['feasible']} passed every configured objective/stability gate; {counts['failed']} failed to evaluate.</p>
<h2>Selected design</h2>{best_rows}
<h2>Stability assessment</h2>{stability_rows or '<p>No complete stability assessment is available.</p>'}
<h2>Automated tests</h2>{tests_html}
<h2>Plots</h2>{images}
<h2>Machine-readable files</h2>
<p><code>summary.json</code>, <code>optimization_checkpoint.json</code>, <code>history.csv</code>, <code>pareto_front.csv</code>, and <code>best_design.json</code>.</p>
<footer>“Pilot-correctable” is a computational screen based on natural-mode divergence time and configured control axes. It does not prove state-space controllability, control authority, or flight certification.</footer>
</body></html>"""


def generate_output_package(
    run: OptimizationRun,
    output_directory: str | Path,
    *,
    configuration: Mapping[str, object] | None = None,
    test_summary: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Write all tables, plots, reports, and the one-line run status."""

    output = Path(output_directory)
    plots_directory = output / "plots"
    plots_directory.mkdir(parents=True, exist_ok=True)
    plot_paths = {
        "Optimization convergence": plots_directory / "01_convergence.png",
        "Objective history": plots_directory / "02_objectives.png",
        "Design frontier": plots_directory / "03_design_frontier.png",
        "Design variables": plots_directory / "04_design_variables.png",
        "Alpha sweep comparison": plots_directory / "05_alpha_sweep.png",
        "Beta sweep comparison": plots_directory / "06_beta_sweep.png",
        "Modal root locus": plots_directory / "07_stability_root_locus.png",
    }
    _convergence_plot(run, plot_paths["Optimization convergence"])
    _objective_history_plot(run, plot_paths["Objective history"])
    _frontier_plot(run, plot_paths["Design frontier"])
    _design_variables_plot(run, plot_paths["Design variables"])
    _sweep_plot(run, "alpha", plot_paths["Alpha sweep comparison"])
    _sweep_plot(run, "beta", plot_paths["Beta sweep comparison"])
    _root_locus_plot(run, plot_paths["Modal root locus"])
    _write_history(run, output / "history.csv")
    _write_frontier(run, output / "pareto_front.csv")

    best = run.best
    (output / "best_design.json").write_text(
        json.dumps(None if best is None else best.to_dict(), indent=2, allow_nan=False), encoding="utf-8"
    )
    optimization_status = "PASS" if best is not None and best.feasible else "FAIL"
    test_status = None if not test_summary else str(test_summary.get("status", "UNKNOWN")).upper()
    status = "PASS" if optimization_status == "PASS" and test_status in {None, "PASS"} else "FAIL"
    relative_plots = {title: path.relative_to(output) for title, path in plot_paths.items()}
    summary: dict[str, object] = {
        "status": status,
        "optimization_status": optimization_status,
        "test_status": test_status,
        "output_directory": str(output.resolve()),
        "best_evaluation_id": None if best is None else best.evaluation_id,
        "best_feasible": bool(best is not None and best.feasible),
        "counts": run.to_dict()["counts"],
        "pareto_evaluation_ids": [record.evaluation_id for record in run.pareto_front],
        "configuration": dict(configuration or {}),
        "tests": dict(test_summary or {}),
        "artifacts": {
            "report": "REPORT.html",
            "history": "history.csv",
            "frontier": "pareto_front.csv",
            "best_design": "best_design.json",
            "checkpoint": "optimization_checkpoint.json",
            "plots": [path.as_posix() for path in relative_plots.values()],
        },
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    (output / "RUN_STATUS.txt").write_text(
        f"{status}\nOptimization: {optimization_status}\n"
        f"Tests: {test_status or 'NOT ATTACHED'}\nBest: {summary['best_evaluation_id']}\n"
        f"Open REPORT.html for the complete report.\n",
        encoding="utf-8",
    )
    (output / "REPORT.html").write_text(
        _html_report(run, relative_plots, status, optimization_status, test_summary),
        encoding="utf-8",
    )
    return summary


__all__ = ["generate_output_package"]
