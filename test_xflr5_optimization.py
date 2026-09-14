import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import Revamped_XFLRPlaneScript as application
from xflr5_core import PlaneXmlEditor
from xflr5_optimization import (
    AnalysisPlan,
    AnalyticDemoBackend,
    DesignVariable,
    GuiXFLRAnalysisBackend,
    HybridAircraftOptimizer,
    HybridSearchSettings,
    ObjectiveSpec,
    SweepDefinition,
)
from test_xflr5_results import modal_fixture


class FakeGuiBackend:
    def __init__(self):
        self.sweep_kinds = []
        self.replacements = []
        self.deleted = 0

    def replace_part_from_xml(self, _plane, filepath, part):
        self.replacements.append((Path(filepath), part))

    def alpha_or_beta_sweep(self, _plane, _name, results_out, **settings):
        kind = settings["sweep_kind"]
        self.sweep_kinds.append(kind)
        start = settings["sweep_start"]
        end = settings["sweep_end"]
        increment = settings["sweep_increment"]
        count = round((end - start) / increment)
        rows = []
        for index in range(count + 1):
            value = start + index * increment
            alpha = value if kind == "alpha" else settings["fixed_angle"]
            beta = settings["fixed_angle"] if kind == "alpha" else value
            rows.append(
                f"{alpha},{beta},{0.3 + 0.1 * alpha},0.03,{-0.01 * beta},{0.02 - 0.01 * alpha},{0.01 * beta},70"
            )
        Path(results_out).write_text(
            "alpha,beta,CL,CD,Cl,Cm,Cn,QInf\n" + "\n".join(rows) + "\n",
            encoding="utf-8",
        )

    def stability_analysis(self, _plane, _name, results_out, **_settings):
        modal_fixture(Path(results_out))

    def delete_polars(self, _plane):
        self.deleted += 1


class GeometryOptimizationTests(unittest.TestCase):
    def test_projected_geometry_editor_changes_multiple_parameters(self):
        source = Path(__file__).with_name("Main_Wing.xml")
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "wing.xml"
            target.write_bytes(source.read_bytes())
            original_area = PlaneXmlEditor.planform_area(target)
            PlaneXmlEditor.apply_geometry(
                target,
                {
                    "span": 48.0,
                    "chord_scale": 0.8,
                    "taper_ratio": 0.5,
                    "sweep_deg": 20.0,
                    "tip_twist_deg": -3.0,
                    "dihedral_deg": 4.0,
                },
            )
            geometry = PlaneXmlEditor.geometry(target)

        self.assertAlmostEqual(geometry["span"], 48.0)
        self.assertAlmostEqual(geometry["taper_ratio"], 0.5)
        self.assertAlmostEqual(geometry["tip_twist_deg"], -3.0)
        self.assertAlmostEqual(geometry["dihedral_deg"], 4.0)
        self.assertLess(geometry["planform_area"], original_area)

    def test_hybrid_search_runs_monte_carlo_then_gradient_and_checkpoints(self):
        variables = [
            DesignVariable("span", 44.0, 72.0, 60.0),
            DesignVariable("sweep_deg", 0.0, 35.0, 18.0),
            DesignVariable("tip_twist_deg", -6.0, 2.0, -2.0),
            DesignVariable("dihedral_deg", 0.0, 8.0, 4.0),
        ]
        objectives = [
            ObjectiveSpec("CL/CD", "maximize", weight=1.0, scale=30.0, label="efficiency"),
            ObjectiveSpec("Cm alpha", "target", target=-0.9, scale=0.3, label="pitch_stability"),
        ]
        settings = HybridSearchSettings(
            monte_carlo_samples=5,
            elite_count=1,
            local_iterations=2,
            random_seed=7,
            max_evaluations=30,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            optimizer = HybridAircraftOptimizer(
                AnalyticDemoBackend(temporary_directory),
                variables,
                objectives,
                temporary_directory,
                settings=settings,
            )
            run = optimizer.optimize()
            checkpoint = json.loads((Path(temporary_directory) / "optimization_checkpoint.json").read_text())

        self.assertEqual(sum(record.phase == "monte_carlo" for record in run.records), 5)
        self.assertTrue(any(record.phase.startswith("gradient") for record in run.records))
        self.assertTrue(any(record.phase.startswith("descent") for record in run.records))
        self.assertLessEqual(run.best.loss, run.records[0].loss)
        self.assertEqual(checkpoint["best_evaluation_id"], run.best.evaluation_id)
        self.assertGreaterEqual(len(run.pareto_front), 1)

    def test_gradient_perturbation_respects_quantized_design_steps(self):
        variables = [DesignVariable("span", 44.0, 45.0, 44.5, step=0.1)]
        objectives = [ObjectiveSpec("CL/CD", "maximize", scale=30.0)]
        settings = HybridSearchSettings(
            monte_carlo_samples=1,
            elite_count=1,
            local_iterations=1,
            gradient_fraction=0.01,
            max_evaluations=5,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            run = HybridAircraftOptimizer(
                AnalyticDemoBackend(temporary_directory),
                variables,
                objectives,
                temporary_directory,
                settings=settings,
            ).optimize()

        self.assertTrue(any(record.phase.startswith("gradient") for record in run.records))

    def test_demo_configuration_supports_all_objective_goal_types(self):
        config = application.default_demo_configuration()
        variables, objectives, _analysis, _stability, _search = application._hybrid_parts(config)

        self.assertTrue(variables)
        self.assertGreaterEqual(len(objectives), 5)
        self.assertIn("maximize", {objective.goal for objective in objectives})
        self.assertIn("minimize", {objective.goal for objective in objectives})
        self.assertIn("target", {objective.goal for objective in objectives})
        self.assertIn("at_least", {objective.goal for objective in objectives})

    def test_gui_backend_runs_distinct_complete_sweeps_and_modal_export(self):
        gui = FakeGuiBackend()
        analysis = AnalysisPlan(
            alpha=SweepDefinition("alpha", -2.0, 2.0, 1.0),
            beta=SweepDefinition("beta", -2.0, 2.0, 1.0, fixed_angle=2.0),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            backend = GuiXFLRAnalysisBackend(
                gui,
                object(),
                Path(__file__).with_name("Main_Wing.xml"),
                temporary_directory,
                analysis,
                reference_area_m2=0.65,
            )
            bundle = backend.evaluate({"span": 60.0, "tip_twist_deg": -2.0}, "eval_0001")

        self.assertEqual(gui.sweep_kinds, ["alpha", "beta"])
        self.assertEqual(gui.deleted, 1)
        self.assertEqual(len(bundle.alpha_rows), 5)
        self.assertEqual(len(bundle.beta_rows), 5)
        self.assertEqual(len(bundle.modal.modes), 8)

    def test_configuration_rejects_modal_requirement_when_modal_run_is_disabled(self):
        config = application.default_demo_configuration()
        config["analysis"]["run_modal"] = False

        with self.assertRaisesRegex(ValueError, "require_modal"):
            application._hybrid_parts(config)

    def test_configuration_rejects_string_booleans_and_conflicting_geometry(self):
        config = application.default_demo_configuration()
        config["objectives"][0]["hard"] = "false"
        with self.assertRaisesRegex(ValueError, "true or false"):
            application._hybrid_parts(config)

        config = application.default_demo_configuration()
        config["backend"] = "xflr"
        config["variables"].append(
            {"name": "root_chord", "minimum": 5.0, "maximum": 10.0, "initial": 7.0}
        )
        with self.assertRaisesRegex(ValueError, "chord_scale with root_chord"):
            application._hybrid_parts(config)

        config = application.default_demo_configuration()
        config["analysis"]["alpha"]["incremnt"] = 1.0
        with self.assertRaisesRegex(ValueError, "Unknown alpha-sweep"):
            application._hybrid_parts(config)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib is installed by requirements.txt")
    def test_complete_report_package_contains_every_required_graph(self):
        from xflr5_reporting import generate_output_package

        config = application.default_demo_configuration()
        variables, objectives, analysis, stability, _search = application._hybrid_parts(config)
        settings = HybridSearchSettings(monte_carlo_samples=3, elite_count=1, local_iterations=0, random_seed=2)
        with tempfile.TemporaryDirectory() as temporary_directory:
            optimizer = HybridAircraftOptimizer(
                AnalyticDemoBackend(temporary_directory, analysis),
                variables,
                objectives,
                temporary_directory,
                stability_criteria=stability,
                settings=settings,
            )
            run = optimizer.optimize()
            tests = {"status": "PASS", "tests_run": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0}
            summary = generate_output_package(run, temporary_directory, test_summary=tests)
            root = Path(temporary_directory)
            artifacts = [root / value for value in summary["artifacts"]["plots"]]

            self.assertEqual(summary["status"], "PASS")
            self.assertTrue((root / "REPORT.html").is_file())
            self.assertTrue((root / "history.csv").is_file())
            self.assertTrue((root / "pareto_front.csv").is_file())
            self.assertEqual(len(artifacts), 7)
            self.assertTrue(all(path.stat().st_size > 1000 for path in artifacts))
            self.assertIn("Automated test status", (root / "REPORT.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
