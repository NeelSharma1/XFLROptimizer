"""The original image/coordinate-based XFLR5 GUI adapter.

This module intentionally preserves the legacy adapter's behavior. The
Windows UI Automation implementation lives in :mod:`xflr5_windows_gui`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Sequence

from xflr5_core import PlaneXmlEditor, XFLRGuiError

try:
    import pyautogui as pg
    import pyperclip
    _GUI_IMPORT_ERROR = None
except Exception as error:  # XML tests and result processing work without GUI packages.
    pg = None
    pyperclip = None
    _GUI_IMPORT_ERROR = error


class XFLRGui:
    """The narrow, XFLR5-layout-specific GUI adapter used by the optimizer."""

    def __init__(
        self,
        project_file: str | Path,
        image_directory: str | Path | None = None,
        startup_timeout: float | None = 300.0,
        clear_existing_polars: bool = False,
    ) -> None:
        if pg is None or pyperclip is None:
            detail = f" Original import error: {_GUI_IMPORT_ERROR}" if _GUI_IMPORT_ERROR else ""
            raise XFLRGuiError(
                "The GUI dependencies could not be imported. Activate the project environment and install "
                "requirements.txt, including a Pillow version compatible with this Python version." + detail
            )
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

    def _wait_for_startup(self, timeout: float | None) -> None:
        """Wait for the user to bring XFLR5 to the foreground.

        PyAutoGUI/PyScreeze versions differ on whether a missing image returns
        ``None`` or raises ``ImageNotFoundException``. Both outcomes mean that
        XFLR5 is not visible yet and must keep the polling loop alive.
        """

        deadline = None if timeout is None else time.monotonic() + timeout
        print("Waiting for XFLR5 to be visible. Switch to XFLR5 and leave its main window onscreen...")
        while deadline is None or time.monotonic() < deadline:
            try:
                found = pg.locate(str(self.image_directory / "start.png"), pg.screenshot(), confidence=0.95)
            except Exception:
                found = None
            if found is not None:
                print("XFLR5 detected; continuing automation.")
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
