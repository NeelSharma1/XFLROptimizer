import csv
import json
import inspect
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import Revamped_XFLRPlaneScript as application
from Revamped_XFLRPlaneScript import (
    AnalysisSpec,
    AircraftCandidate,
    AircraftOptimizer,
    ConstraintError,
    convert_length,
    OptimizationConstraints,
    PlaneXmlEditor,
    XFLRGui,
    parse_result_table,
    score_result,
)


class RevampedXflrTests(unittest.TestCase):
    def test_length_conversion_uses_xml_native_units(self):
        self.assertAlmostEqual(convert_length("5 feet", 0.0254), 60.0)
        self.assertAlmostEqual(convert_length("1 m", 0.0254), 39.3700787402)
        self.assertAlmostEqual(convert_length("60", 0.0254), 60.0)
        self.assertAlmostEqual(PlaneXmlEditor.length_unit_to_meter("Main_Wing.xml"), 0.0254)

    def test_sweep_exposes_analysis_parameters_with_defaults(self):
        parameters = inspect.signature(XFLRGui.alpha_or_beta_sweep).parameters
        expected = {
            "speed": 70.0,
            "sweep_start": -13.0,
            "sweep_end": 13.0,
            "sweep_increment": 0.5,
            "fixed_angle": 0.0,
            "wait_seconds": 20.0,
        }
        self.assertEqual({name: parameters[name].default for name in expected}, expected)

    def test_component_xml_airfoil_and_span_edits(self):
        source = Path(__file__).with_name("Main_Wing.xml")
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "wing.xml"
            target.write_bytes(source.read_bytes())
            self.assertAlmostEqual(PlaneXmlEditor.span(target), 60.0)
            PlaneXmlEditor.replace_airfoils(target, "N22", "NACA 0012", (3, None))
            PlaneXmlEditor.set_span(target, 48.0)
            self.assertAlmostEqual(PlaneXmlEditor.span(target), 48.0)
            text = target.read_text(encoding="utf-8")
            self.assertIn("<Left_Side_FoilName>N22</Left_Side_FoilName>", text)
            self.assertIn("<Right_Side_FoilName>NACA 0012</Right_Side_FoilName>", text)

    def test_constraints_reject_span_and_airfoil_outside_subset(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            optimizer = AircraftOptimizer(
                gui=None,
                work_directory=temporary_directory,
                constraints=OptimizationConstraints(span_limit=50, allowed_airfoils=("N22",)),
                foil_directory=None,
            )
            with self.assertRaises(ConstraintError):
                optimizer.validate(AircraftCandidate("bad-foil", left_airfoil="NACA 0012"))
            with self.assertRaises(ConstraintError):
                optimizer.validate(AircraftCandidate("bad-span", left_airfoil="N22", span=60))

    def test_csv_result_scoring(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            result_file = Path(temporary_directory) / "result.csv"
            result_file.write_text("Alpha,CL,CD\n0,0.3,0.03\n2,0.8,0.04\n", encoding="utf-8")
            self.assertEqual(len(parse_result_table(result_file)), 2)
            self.assertAlmostEqual(score_result(result_file, "CL/CD"), 20.0)

    def test_optimizer_runs_feasible_candidates_and_checkpoints(self):
        source = Path(__file__).with_name("Main_Wing.xml")

        class FakeGui:
            def export_wing(self, _plane, destination, _part):
                shutil.copy2(source, destination)

            def replace_part_from_xml(self, _plane, _path, _part):
                return None

            def alpha_or_beta_sweep(self, _plane, name, result_file, **_analysis):
                value = 20.0 if name.startswith("N22") else 15.0
                Path(result_file).write_text(f"CL,CD\n{value},1\n", encoding="utf-8")

            def delete_polars(self, _plane):
                return None

        with tempfile.TemporaryDirectory() as temporary_directory:
            optimizer = AircraftOptimizer(
                FakeGui(),
                temporary_directory,
                OptimizationConstraints(span_limit=60, allowed_airfoils=("N22", "NACA 0012")),
                wait_seconds=0,
                metric="CL/CD",
            )
            candidates = optimizer.grid_candidates(["N22", "NACA 0012"], [48])
            result = optimizer.optimize((0, 0), candidates)
            self.assertEqual([record.status for record in result.records], ["ok", "ok"])
            self.assertEqual(result.best.candidate.left_airfoil, "N22")
            checkpoint = json.loads((Path(temporary_directory) / "optimization_results.json").read_text())
            self.assertEqual(len(checkpoint["records"]), 2)

    def test_optimizer_rejects_a_baseline_span_over_the_limit(self):
        source = Path(__file__).with_name("Main_Wing.xml")

        class FakeGui:
            def export_wing(self, _plane, destination, _part):
                shutil.copy2(source, destination)

        with tempfile.TemporaryDirectory() as temporary_directory:
            optimizer = AircraftOptimizer(
                FakeGui(),
                temporary_directory,
                OptimizationConstraints(span_limit=50, allowed_airfoils=("N22",)),
                wait_seconds=0,
            )
            result = optimizer.optimize((0, 0), optimizer.grid_candidates(["N22"]))
            self.assertEqual(result.records[0].status, "failed")
            self.assertIn("Realized span 60 exceeds limit 50", result.records[0].message)

    def test_optimizer_configuration_file_loads_settings_without_terminal_prompts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project.xfl"
            project.write_text("project", encoding="utf-8")
            foil_directory = root / "foils"
            foil_directory.mkdir()
            (foil_directory / "n22.dat").write_text("N22\n0 0\n", encoding="utf-8")
            config = root / "optimizer.txt"
            config.write_text(
                "\n".join(
                    (
                        "project=project.xfl",
                        "foil_directory=foils",
                        "airfoils=N22",
                        "part=elevator",
                        "plane_index=2",
                        "spans=48, 60",
                        "span_limit=60",
                        "speed=55",
                        "sweep_start=-5",
                        "sweep_end=10",
                        "sweep_increment=0.25",
                        "fixed_angle=1",
                        "wait_seconds=3",
                        "metric=CL/CD",
                        "maximize=no",
                        "clear_polars=false",
                        "output=results",
                    )
                ),
                encoding="utf-8",
            )

            settings = application._settings_from_optimizer_config(config)

        self.assertEqual(settings["project"], project)
        self.assertEqual(settings["foil_directory"], foil_directory)
        self.assertEqual(settings["airfoils"], ["N22"])
        self.assertEqual(settings["part"], "elevator")
        self.assertEqual(settings["plane_index"], 2)
        self.assertEqual(settings["spans"], ["48", "60"])
        self.assertEqual(settings["span_limit"], "60")
        self.assertEqual(settings["analysis"], AnalysisSpec(55.0, -5.0, 10.0, 0.25, 1.0, 3.0))
        self.assertFalse(settings["maximize"])
        self.assertFalse(settings["clear_polars"])
        self.assertEqual(settings["output"], root / "results")

    def test_cancelled_configuration_picker_uses_terminal_settings(self):
        terminal_settings = {"project": Path("terminal.xfl")}
        with patch.object(application, "_select_optimizer_config_file", return_value=None), patch.object(
            application, "_interactive_settings_terminal", return_value=terminal_settings
        ) as terminal:
            self.assertIs(application._interactive_settings(), terminal_settings)
        terminal.assert_called_once_with()

    def test_configuration_parser_rejects_unknown_options(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "optimizer.txt"
            config.write_text("unknown=value\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown optimizer configuration option"):
                application._read_optimizer_config(config)


if __name__ == "__main__":
    unittest.main()
