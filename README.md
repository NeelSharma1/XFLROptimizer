# XFLR5 Aircraft Optimizer

This project now has two execution paths:

- A full, deterministic demo that runs on Windows, macOS, and Linux without XFLR5. It exercises the optimizer, stability gates, parsers, checkpointing, Pareto analysis, and every output plot.
- A real XFLR5 path focused on Windows v6.62. It edits a clean component XML for every design, runs separate Type-1 alpha and Type-5 beta sequences, exports their polar tables, runs a Type-7 stability sequence, and exports the selected stability operating point as TXT for eigenvalue extraction.

The program never treats a normal polar CSV as modal evidence. If an alpha sweep, beta sweep, all eight modal roots, or the configured stability checks are missing, that design does not pass.

## Fastest complete run on Windows

Double-click `run_windows.bat`, or run:

```bat
run_windows.bat
```

It installs the required dependencies, runs the complete test suite, creates a timestamped demo optimization under `runs\`, and opens `REPORT.html`. The first line of `RUN_STATUS.txt` is `PASS` or `FAIL`.

On macOS/Linux, the equivalent command is:

```sh
./run_unix.sh
```

To test the report pipeline directly:

```sh
python Revamped_XFLRPlaneScript.py --demo --output optimization_output
```

## Running against XFLR5 on Windows

1. Install XFLR5 v6.62 and Python 3.10 or newer.
2. In XFLR5, define valid aircraft mass, center of gravity, and all inertia values. Type-7 results are not meaningful without these.
3. Define the real control surfaces and verify their directions. `available_controls` describes what the pilot can command; it does not create a flap in XFLR5.
4. Generate viscous foil polars that cover the Reynolds/angle range if viscous plane analysis is enabled in XFLR5.
5. Copy `optimizer_config.windows.example.json`, set `project`, `xflr5_executable`, component geometry bounds, reference area, targets, and stability thresholds.
6. Close unrelated XFLR5 instances, leave the XFLR5 UI unobscured, and run the complete test-plus-real-analysis workflow:

```bat
run_windows.bat --config optimizer_config.windows.json
```

That creates a timestamped directory under `runs`, embeds the automated test summary in the same report, and writes `RUN_ERROR.txt` plus a `FAIL` status if XFLR setup or GUI analysis aborts. To bypass the test wrapper and use the configuration's own output path, run `py -3 Revamped_XFLRPlaneScript.py --config optimizer_config.windows.json` directly.

The Windows adapter uses UI Automation and exact upstream English v6.62 control names, with deterministic fallbacks matching that dialog layout. It forces Sequence and Store OpPoint on, waits until XFLR re-enables the Analyze button, and rejects an export if any requested alpha/beta point is missing. If the installed XFLR build/localization exposes different accessibility properties, run the adapter's `dump_uia_tree()` diagnostic and update the aliases in `xflr5_windows_gui.py`. XFLR5 and Python must run at the same Windows privilege level; the adapter handles the normal elevation restart case. Analysis speed is entered in XFLR's currently displayed speed unit; exported `QInf` values are used for the force calculations.

XFLR5 does not expose batch plane analysis through its legacy script interface, so real macOS/Linux plane automation still uses the image/keyboard adapter. Alpha and beta sweeps are supported there. Automated Type-7 operating-point export deliberately fails closed on that adapter rather than reporting unverified dynamic stability. The complete optimizer, parsers, tests, plots, and demo remain cross-platform; unattended modal automation is currently Windows-first.

## Algorithm

The search has two explicit stages:

1. Reproducible Latin-hypercube Monte Carlo sampling covers every continuous range while distributing categorical choices such as airfoils.
2. The best Monte Carlo designs seed projected finite-difference gradient descent. Central differences estimate the local gradient, bounds/steps project each update back into the legal design space, and backtracking reduces the learning rate when a trial does not improve.

Only unique designs invoke XFLR5; repeats are cached. `max_evaluations` is a hard cap. Every completed design atomically updates `optimization_checkpoint.json`, so progress is inspectable during a long GUI run.

The reported Pareto frontier contains non-dominated designs that passed every hard objective and stability gate. The single selected design minimizes the configured weighted loss over that feasible set.

## Design variables

Categorical variables use `choices`; numeric variables use `minimum`, `maximum`, and optional `initial`/`step`.

Supported XML geometry variable names are:

| Name | Meaning |
|---|---|
| `airfoil`, `left_airfoil`, `right_airfoil` | XFLR foil names |
| `span` | Full span in native component XML units |
| `chord_scale` | Multiplier applied to all section chords |
| `root_chord`, `tip_chord` | Chord in native XML units |
| `taper_ratio` | Tip chord / root chord |
| `sweep_deg` | Quarter-chord sweep |
| `root_twist_deg`, `tip_twist_deg` | Linear spanwise twist endpoints |
| `dihedral_deg` | Component section dihedral |
| `tilt_deg` | XFLR component tilt |
| `x_position`, `y_position`, `z_position` | Component position in native XML units |

Every candidate starts from the unchanged baseline XML, so geometry changes never accumulate from a previous design.

## Objectives and conditions

Any number of objectives can be active concurrently. A JSON objective supports:

- `goal`: `maximize`, `minimize`, `target`, `at_least`, or `at_most`
- `target`, `weight`, and `scale`
- `sweep`: `alpha` or `beta`
- `reducer`: `max`, `min`, `mean`, `absmax`, `absmin`, or `last`
- `conditions`: row filters such as `{"alpha": [-2, 8]}` or an exact value
- `hard` and `tolerance`: turn a target/limit into a feasibility gate

Direct XFLR fields include `CL`, `CD`, `CDi`, `CDv`, `CY`, `Cl`, `Cm`, `Cn`, `Cni`, `QInf`, and `XCP`. Case matters: `CL` is lift coefficient; `Cl` is rolling-moment coefficient.

Derived metrics include:

- `Lift`, `Drag`, and `CL/CD`
- `CL alpha`, `Cm alpha`, `Cn beta`, `Cl beta`, and `CY beta` (derivatives are per radian)
- `static margin` (calculated as `-Cm_alpha / CL_alpha`)
- `trim alpha` and `trim CL`
- `max mode real`, `minimum damping ratio`, and `minimum time to double`

Force objectives require `reference_area_m2`; if omitted, the optimized component's trapezoidal planform area is used. For a full-aircraft lift/drag target, explicitly provide the XFLR polar's reference area.

## Stability and pilot-control screening

Static screening checks:

- negative `Cm_alpha` for longitudinal static stability;
- positive `Cn_beta` for directional static stability;
- negative `Cl_beta` for lateral static stability;
- a bracketed `Cm = 0` trim point with the required positive `CL`.

The static derivatives use the configurable `static_alpha_range` and `static_beta_range`, while trim is located over the complete alpha sweep. `required_control_axes` must be a subset of the declared `available_controls` for a design to pass. Dynamic screening parses four longitudinal roots and four lateral roots from XFLR's TXT operating-point export, classifies phugoid, short-period, Dutch-roll, roll, and spiral modes, and checks every real part. A configured slowly divergent phugoid/spiral may pass the pilot-correctable screen only when its doubling time clears the threshold and the pilot has an applicable pitch/roll/yaw control. This does not prove a state-space controllability matrix or sufficient control authority; those require a validated control model/forced-response analysis and ultimately flight testing.

Set both `analysis.run_modal` and `stability.require_modal` to `false` only when intentionally running the cross-platform static-only path. The report then leaves modal availability and dynamic stability marked as unverified instead of claiming they passed.

## Output package

Each run contains:

- `RUN_STATUS.txt` and `REPORT.html`
- `summary.json`, `best_design.json`, and the atomic `optimization_checkpoint.json`
- `history.csv` and `pareto_front.csv`
- each design XML and raw alpha/beta/modal export
- seven 2D Matplotlib figures: convergence, all objective histories, Pareto frontier (including an all-objective normalized view), tested design variables, alpha comparison, beta comparison, and modal root locus
- `tests/TEST_RESULTS.txt` and `tests/test_results.json` when run through `run_default.py`

## Tests

Run all tests and produce a compact pass/fail artifact with:

```sh
python run_test_suite.py --output test_output
```

The suite covers XML geometry projection, unit conversion, localized table parsing, case-sensitive XFLR coefficients, sweep completeness, derivatives, modal classification, static/dynamic/pilot gates, multi-objective scoring, Monte Carlo sampling, gradient refinement, Pareto extraction, checkpoints, report plots, configuration parsing, a mocked full GUI-backend pass, legacy behavior, and Windows UI Automation fallbacks. It cannot replace a live smoke test against the exact XFLR5 Windows build, project, foil database, and UI accessibility tree you will use.
