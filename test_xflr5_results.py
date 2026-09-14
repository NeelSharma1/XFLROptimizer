import math
import tempfile
import unittest
from pathlib import Path

from xflr5_results import (
    MetricContext,
    StabilityCriteria,
    assess_stability,
    linear_slope,
    metric_values,
    parse_modal_report,
    parse_result_table,
    validate_sweep_coverage,
)


def modal_fixture(path: Path, *, spiral_real: float = -0.03) -> Path:
    path.write_text(
        "XFLR5 v6.62\n\n"
        "   ___Longitudinal modes____\n\n"
        "      Eigenvalue:  -0.03+0.5i | -0.03+-0.5i | -1.2+5.0i | -1.2+-5.0i\n\n"
        "   ___Lateral modes____\n\n"
        f"      Eigenvalue:  -0.2+2.0i | -0.2+-2.0i | -2.5+0i | {spiral_real}+0i\n",
        encoding="utf-8",
    )
    return path


class ResultParsingTests(unittest.TestCase):
    def test_xflr_preamble_and_case_distinct_coefficients_are_parsed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = Path(temporary_directory) / "polar.csv"
            result.write_text(
                "XFLR5 v6.62\n\nPlane name :, Example\n"
                "alpha, Beta, CL, CD, Cl, Cm, Cn, QInf\n"
                "0, 0, 0.4, 0.02, -0.01, 0.03, 0.0, 20\n"
                "2, 0, 0.6, 0.03, -0.02, -0.01, 0.0, 20\n",
                encoding="utf-8",
            )
            rows = parse_result_table(result)

        self.assertEqual(rows[0]["CL"], 0.4)
        self.assertEqual(rows[0]["Cl"], -0.01)
        self.assertEqual(rows[1]["alpha"], 2.0)

    def test_semicolon_export_accepts_decimal_comma_locale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = Path(temporary_directory) / "polar.csv"
            result.write_text(
                "alpha;Beta;CL;CD;Cm\n0,0;0,0;0,40;0,020;0,03\n2,0;0,0;0,60;0,030;-0,01\n",
                encoding="utf-8",
            )
            rows = parse_result_table(result)

        self.assertEqual(rows[0]["CL"], 0.4)
        self.assertEqual(rows[1]["Cm"], -0.01)

    def test_derivative_and_physical_force_metrics(self):
        rows = [
            {"alpha": -1.0, "CL": 0.2, "CD": 0.02, "Cm": 0.02, "QInf": 10.0},
            {"alpha": 1.0, "CL": 0.4, "CD": 0.03, "Cm": -0.02, "QInf": 10.0},
        ]

        cm_alpha = metric_values("Cm alpha", alpha_rows=rows, beta_rows=[], sweep="alpha")
        lift = metric_values(
            "Lift",
            alpha_rows=rows,
            beta_rows=[],
            sweep="alpha",
            context=MetricContext(1.0, 2.0, 10.0),
        )

        self.assertAlmostEqual(cm_alpha[0], -0.02 * 180.0 / math.pi)
        self.assertEqual(lift, [20.0, 40.0])

    def test_modal_report_classifies_all_eight_roots(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            modal = parse_modal_report(modal_fixture(Path(temporary_directory) / "modal.txt"))

        self.assertEqual(len(modal.modes), 8)
        self.assertEqual({mode.name for mode in modal.modes}, {"phugoid", "short_period", "dutch_roll", "roll", "spiral"})
        self.assertLess(modal.max_real_part, 0.0)

    def test_stability_gate_requires_static_trim_and_modal_evidence(self):
        alpha = [
            {"alpha": -2.0, "CL": 0.1, "Cm": 0.04},
            {"alpha": 0.0, "CL": 0.3, "Cm": 0.02},
            {"alpha": 2.0, "CL": 0.5, "Cm": 0.0},
            {"alpha": 4.0, "CL": 0.7, "Cm": -0.02},
        ]
        beta = [
            {"beta": -2.0, "Cn": -0.02, "Cl": 0.02},
            {"beta": 0.0, "Cn": 0.0, "Cl": 0.0},
            {"beta": 2.0, "Cn": 0.02, "Cl": -0.02},
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            modal = parse_modal_report(modal_fixture(Path(temporary_directory) / "modal.txt"))
            assessment = assess_stability(alpha, beta, modal)
            missing = assess_stability(alpha, beta, None)

        self.assertTrue(assessment.feasible)
        self.assertFalse(missing.feasible)
        self.assertFalse(missing.modal_complete)

    def test_slow_spiral_can_be_screened_as_pilot_correctable(self):
        alpha = [{"alpha": -2.0, "CL": 0.1, "Cm": 0.04}, {"alpha": 2.0, "CL": 0.5, "Cm": 0.0}]
        beta = [{"beta": -2.0, "Cn": -0.02, "Cl": 0.02}, {"beta": 2.0, "Cn": 0.02, "Cl": -0.02}]
        with tempfile.TemporaryDirectory() as temporary_directory:
            modal = parse_modal_report(modal_fixture(Path(temporary_directory) / "modal.txt", spiral_real=0.05))
            criteria = StabilityCriteria(
                min_time_to_double_seconds=10.0,
                available_controls=("roll",),
                required_control_axes=("roll",),
            )
            assessment = assess_stability(alpha, beta, modal, criteria)

        self.assertFalse(assessment.dynamic_stable)
        self.assertTrue(assessment.pilot_controllable)
        self.assertTrue(assessment.feasible)

    def test_modal_gate_can_be_explicitly_disabled_without_claiming_dynamic_stability(self):
        alpha = [{"alpha": -2.0, "CL": 0.1, "Cm": 0.04}, {"alpha": 2.0, "CL": 0.5, "Cm": 0.0}]
        beta = [{"beta": -2.0, "Cn": -0.02, "Cl": 0.02}, {"beta": 2.0, "Cn": 0.02, "Cl": -0.02}]

        assessment = assess_stability(alpha, beta, None, StabilityCriteria(require_modal=False))

        self.assertTrue(assessment.feasible)
        self.assertFalse(assessment.modal_complete)
        self.assertFalse(assessment.dynamic_stable)

    def test_missing_required_pilot_axis_fails_the_control_screen(self):
        alpha = [{"alpha": -2.0, "CL": 0.1, "Cm": 0.04}, {"alpha": 2.0, "CL": 0.5, "Cm": 0.0}]
        beta = [{"beta": -2.0, "Cn": -0.02, "Cl": 0.02}, {"beta": 2.0, "Cn": 0.02, "Cl": -0.02}]
        criteria = StabilityCriteria(
            require_modal=False,
            available_controls=("pitch", "roll"),
            required_control_axes=("pitch", "roll", "yaw"),
        )

        assessment = assess_stability(alpha, beta, None, criteria)

        self.assertFalse(assessment.control_axes_available)
        self.assertFalse(assessment.feasible)
        self.assertTrue(any("yaw" in reason for reason in assessment.reasons))

    def test_sweep_coverage_reports_missing_solver_points(self):
        rows = [{"alpha": -1.0}, {"alpha": 0.0}, {"alpha": 2.0}]

        with self.assertRaisesRegex(ValueError, "1 requested point"):
            validate_sweep_coverage(rows, "alpha", [-1.0, 0.0, 1.0, 2.0], tolerance=0.01)


if __name__ == "__main__":
    unittest.main()
