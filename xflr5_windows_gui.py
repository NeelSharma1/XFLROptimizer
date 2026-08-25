"""Deterministic Windows UI Automation adapter for XFLR5.

The adapter deliberately uses pywinauto's ``uia`` backend. It never takes a
screenshot, searches an image, or converts a control into an absolute screen
coordinate. Buttons are invoked through UI Automation patterns and text is
written through the Value pattern or focused keyboard input.

XFLR5 is a Qt application, and the exact AutomationId values can vary between
versions. The locators therefore prefer known AutomationIds, then exact
English control names and control types. A missing or ambiguous locator is a
hard error rather than a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable, Iterable

from xflr5_core import PlaneXmlEditor, XFLRGuiError

try:
    from pywinauto import Desktop
    from pywinauto.application import Application
    from pywinauto.keyboard import send_keys

    _UIA_IMPORT_ERROR = None
except Exception as error:  # Importing the XML-only portions must work off Windows.
    Desktop = None
    Application = None
    send_keys = None
    _UIA_IMPORT_ERROR = error


@dataclass(frozen=True)
class UiaLocator:
    """Exact UIA properties used to identify one control."""

    control_type: str | None = None
    titles: tuple[str, ...] = ()
    auto_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WindowsPlaneRef:
    """Stable reference to a top-level plane tree item.

    ``index`` preserves the current CLI's zero-based plane selection. ``name``
    lets the adapter verify that the tree has not been unexpectedly reordered
    before an operation is performed.
    """

    index: int
    name: str


class _UiaAmbiguousError(XFLRGuiError):
    """Internal distinction between an absent and an ambiguous control."""


_LOCATORS: dict[str, UiaLocator] = {
    "file_menu": UiaLocator("MenuItem", titles=("File",)),
    "open_menu": UiaLocator("MenuItem", titles=("Open", "Open Project", "Open Project File")),
    "other_button": UiaLocator("Button", titles=("Other...", "Other", "Browse...")),
    "open_button": UiaLocator("Button", titles=("Open", "Import", "Select")),
    "save_button": UiaLocator("Button", titles=("Save", "Export", "Save As")),
    "file_name_edit": UiaLocator(
        "Edit",
        titles=("File name:", "File name", "Filename", "File Name"),
        auto_ids=("fileNameEdit", "filenameEdit", "1148"),
    ),
    "ok_button": UiaLocator("Button", titles=("OK", "Ok", "Accept")),
    "yes_button": UiaLocator("Button", titles=("Yes", "Replace", "Confirm")),
    "sequence_toggle": UiaLocator(
        "CheckBox",
        titles=("Sequence", "Sequence analysis", "Sequence On", "Sequence Off"),
        auto_ids=("sequence", "sequenceCheckBox", "sequenceButton"),
    ),
    "start_analysis": UiaLocator(
        "Button",
        titles=("Start Analysis", "Analyze", "Start", "Run Analysis"),
        auto_ids=("startAnalysis", "startAnalysisButton", "analyzeButton"),
    ),
    "speed_edit": UiaLocator(
        "Edit",
        titles=("Speed", "Speed (m/s)", "Velocity"),
        auto_ids=("speed", "speedEdit", "velocityEdit"),
    ),
    "fixed_angle_edit": UiaLocator(
        "Edit",
        titles=("Fixed angle", "Fixed Angle", "Angle", "Alpha"),
        auto_ids=("fixedAngle", "fixedAngleEdit", "alphaEdit"),
    ),
    "analysis_name_edit": UiaLocator(
        "Edit",
        titles=("Analysis name", "Analysis Name", "Name", "Polar name", "Polar Name"),
        auto_ids=("analysisName", "analysisNameEdit", "polarNameEdit"),
    ),
    "sweep_start_edit": UiaLocator(
        "Edit",
        titles=("Start", "Start angle", "Sweep start", "Alpha start"),
        auto_ids=("sweepStart", "startAngle", "alphaStart"),
    ),
    "sweep_end_edit": UiaLocator(
        "Edit",
        titles=("End", "End angle", "Sweep end", "Alpha end"),
        auto_ids=("sweepEnd", "endAngle", "alphaEnd"),
    ),
    "sweep_increment_edit": UiaLocator(
        "Edit",
        titles=("Increment", "Angle increment", "Sweep increment", "Alpha increment"),
        auto_ids=("sweepIncrement", "angleIncrement", "alphaIncrement"),
    ),
    "export_results": UiaLocator(
        "MenuItem",
        titles=("Export Results", "Export result", "Export Polar Results"),
        auto_ids=("exportResults", "exportResultsMenuItem"),
    ),
    "delete_polars": UiaLocator(
        "MenuItem",
        titles=("Delete Polars", "Delete polars", "Delete Polar"),
        auto_ids=("deletePolars", "deletePolarsMenuItem"),
    ),
}


class WindowsXFLRGui:
    """XFLR5 automation through Windows UI Automation.

    The public method names intentionally match the optimizer-facing portion
    of the legacy ``XFLRGui`` adapter. The ``plane`` value is a
    :class:`WindowsPlaneRef`, not a coordinate pair.
    """

    _WINDOW_TITLE_RE = r"(?i).*xflr5.*"
    _POLL_INTERVAL = 0.10

    def __init__(
        self,
        project_file: str | Path,
        executable_path: str | Path | None = None,
        startup_timeout: float | None = 300.0,
        clear_existing_polars: bool = False,
    ) -> None:
        if Application is None or Desktop is None or send_keys is None:
            detail = f" Original import error: {_UIA_IMPORT_ERROR}" if _UIA_IMPORT_ERROR else ""
            raise XFLRGuiError(
                "The Windows UI Automation dependency could not be imported. Install requirements.txt on Windows."
                + detail
            )
        self.project_file = Path(project_file)
        if not self.project_file.is_file():
            raise FileNotFoundError(self.project_file)
        self.sequence_on = False
        self._startup_timeout = startup_timeout
        self._app: Any = None
        self._desktop: Any = None
        self._main: Any = None
        self._last_plane: WindowsPlaneRef | None = None
        self._connect(executable_path, startup_timeout)
        self.open_project_file(self.project_file)
        self._wait_for(lambda: self._plane_items() or None, "XFLR5 plane tree", startup_timeout)
        self.plane_location = self.plane_coordinates_from_index(0)
        self.reset_to_plane_level()
        if clear_existing_polars:
            self.delete_polars(self.plane_location)

    # ---------- process and UIA tree helpers ----------

    def _connect(self, executable_path: str | Path | None, timeout: float | None) -> None:
        self._desktop = Desktop(backend="uia")
        timeout_value = 30.0 if timeout is None else timeout
        executable = Path(executable_path) if executable_path is not None else None
        if executable is not None and not executable.is_file():
            raise FileNotFoundError(executable)

        self._app = Application(backend="uia")
        if executable is not None:
            try:
                self._app.connect(path=str(executable), timeout=min(timeout_value, 5.0))
            except Exception:
                self._app = Application(backend="uia").start(str(executable))
        else:
            try:
                self._app.connect(title_re=self._WINDOW_TITLE_RE, timeout=timeout_value)
            except Exception as error:
                raise XFLRGuiError(
                    "Could not connect to XFLR5. Start xflr5.exe first, or provide executable_path."
                ) from error

        try:
            main_spec = self._app.window(title_re=self._WINDOW_TITLE_RE)
            main_spec.wait("visible enabled ready", timeout=timeout_value)
            self._main = main_spec.wrapper_object()
        except Exception as error:
            raise XFLRGuiError("Connected to a process, but its XFLR5 main window was not accessible.") from error

    def _wait_for(
        self,
        predicate: Callable[[], Any],
        description: str,
        timeout: float | None = None,
    ) -> Any:
        limit = self._startup_timeout if timeout is None else timeout
        deadline = None if limit is None else time.monotonic() + max(0.0, limit)
        while deadline is None or time.monotonic() <= deadline:
            result = predicate()
            if result:
                return result
            time.sleep(self._POLL_INTERVAL)
        raise XFLRGuiError(f"Timed out waiting for {description}.")

    @staticmethod
    def _element_key(control: Any) -> object:
        info = getattr(control, "element_info", None)
        for attribute in ("runtime_id", "handle"):
            value = getattr(info, attribute, None)
            if value is not None:
                return (attribute, repr(value))
        return id(control)

    @classmethod
    def _deduplicate(cls, controls: Iterable[Any]) -> list[Any]:
        result: list[Any] = []
        seen: set[object] = set()
        for control in controls:
            key = cls._element_key(control)
            if key not in seen:
                seen.add(key)
                result.append(control)
        return result

    @staticmethod
    def _descendants(parent: Any, **criteria: Any) -> list[Any]:
        try:
            return list(parent.descendants(**criteria))
        except Exception:
            return []

    @classmethod
    def _locate_once(cls, parent: Any, locator: UiaLocator) -> Any | None:
        base = {} if locator.control_type is None else {"control_type": locator.control_type}
        for auto_id in locator.auto_ids:
            matches = cls._deduplicate(cls._descendants(parent, **base, auto_id=auto_id))
            if len(matches) > 1:
                raise _UiaAmbiguousError(f"UIA AutomationId {auto_id!r} matched multiple controls.")
            if matches:
                return matches[0]
        for title in locator.titles:
            matches = cls._deduplicate(cls._descendants(parent, **base, title=title))
            if len(matches) > 1:
                raise _UiaAmbiguousError(f"UIA title {title!r} matched multiple controls.")
            if matches:
                return matches[0]
        if not locator.auto_ids and not locator.titles:
            matches = cls._deduplicate(cls._descendants(parent, **base))
            if len(matches) > 1:
                raise _UiaAmbiguousError(f"UIA locator {locator!r} matched multiple controls.")
            if matches:
                return matches[0]
        return None

    def _control(
        self,
        parent: Any,
        locator: UiaLocator,
        description: str,
        timeout: float | None = 10.0,
    ) -> Any:
        return self._wait_for(
            lambda: self._locate_once(parent, locator),
            description,
            timeout,
        )

    def _visible_windows(self) -> list[Any]:
        try:
            windows = list(self._app.windows())
        except Exception:
            return []
        visible: list[Any] = []
        main_handle = getattr(self._main, "handle", None)
        for window in windows:
            try:
                if not window.is_visible():
                    continue
            except Exception:
                continue
            if main_handle is None or getattr(window, "handle", None) != main_handle:
                visible.append(window)
        return visible

    def _control_any_window(self, locator: UiaLocator, description: str, timeout: float | None = 10.0) -> Any:
        def locate() -> Any | None:
            parents = [self._main, *self._visible_windows()]
            for parent in parents:
                found = self._locate_once(parent, locator)
                if found is not None:
                    return found
            return None

        return self._wait_for(locate, description, timeout)

    def _window_with_control(self, locator: UiaLocator) -> Any | None:
        for window in self._visible_windows():
            if self._locate_once(window, locator) is not None:
                return window
        return None

    def _wait_window_with_control(self, locator: UiaLocator, description: str, timeout: float | None = 10.0) -> Any:
        return self._wait_for(lambda: self._window_with_control(locator), description, timeout)

    def _secondary_window(self, preferred_titles: tuple[str, ...] = ()) -> Any | None:
        windows = self._visible_windows()
        if preferred_titles:
            for window in windows:
                try:
                    title = window.window_text()
                except Exception:
                    title = ""
                if title in preferred_titles:
                    return window
        return windows[0] if len(windows) == 1 else None

    def _wait_secondary_window(self, description: str = "XFLR5 dialog", timeout: float | None = 10.0) -> Any:
        return self._wait_for(lambda: self._secondary_window(), description, timeout)

    # ---------- deterministic input helpers ----------

    @staticmethod
    def _invoke(control: Any, description: str) -> None:
        invoke = getattr(control, "invoke", None)
        if not callable(invoke):
            raise XFLRGuiError(f"{description} does not expose the UIA Invoke pattern.")
        try:
            invoke()
        except Exception as error:
            raise XFLRGuiError(f"Could not invoke {description} through UI Automation.") from error

    @staticmethod
    def _set_text(control: Any, value: str, description: str) -> None:
        setter_error: Exception | None = None
        try:
            setter = getattr(control, "set_edit_text", None)
            if callable(setter):
                try:
                    setter(value)
                    return
                except Exception as error:
                    setter_error = error
            control.set_focus()
            send_keys("^a")
            send_keys(value)
        except Exception as error:
            if setter_error is not None:
                error = setter_error
            raise XFLRGuiError(f"Could not set {description} through UI Automation.") from error

    @staticmethod
    def _send_keys(keys: str, target: Any | None = None) -> None:
        try:
            if target is not None:
                target.set_focus()
            send_keys(keys)
        except Exception as error:
            raise XFLRGuiError(f"Could not send deterministic keyboard sequence {keys!r}.") from error

    def _invoke_menu_path(self, titles: tuple[str, ...]) -> None:
        parent = self._main
        try:
            for title in titles:
                control = self._control(
                    parent,
                    UiaLocator("MenuItem", titles=(title,)),
                    f"menu item {title!r}",
                    timeout=1.0,
                )
                self._invoke(control, f"menu item {title!r}")
                parent = self._main
        except _UiaAmbiguousError:
            raise
        except XFLRGuiError:
            # Menu accelerators are deterministic and useful for Qt menu items
            # which do not expose their popup menu through UIA.
            self._send_keys("%f", self._main)
            for title in titles[1:]:
                accelerator = {"Open": "o", "Open Project": "o", "Save": "s"}.get(title)
                if accelerator is None:
                    raise
                self._send_keys(accelerator)

    def _file_dialog(self, action: str, timeout: float | None = 10.0) -> Any:
        locator = _LOCATORS["open_button"] if action == "open" else _LOCATORS["save_button"]

        def locate() -> Any | None:
            for window in self._visible_windows():
                if self._locate_once(window, _LOCATORS["file_name_edit"]) is not None:
                    if self._locate_once(window, locator) is not None:
                        return window
            return None

        return self._wait_for(locate, f"{action} file dialog", timeout)

    def _complete_file_dialog(self, filepath: str | Path, action: str) -> None:
        dialog = self._file_dialog(action)
        filename = self._control(dialog, _LOCATORS["file_name_edit"], "file-name field")
        self._set_text(filename, str(Path(filepath)), "file name")
        button_key = "open_button" if action == "open" else "save_button"
        button = self._control(dialog, _LOCATORS[button_key], f"{action} file button")
        self._invoke(button, f"{action} file button")

    def _analysis_edit(self, parent: Any, key: str, description: str, fallback_index: int | None = None) -> Any:
        locator = _LOCATORS[key]
        try:
            return self._control(parent, locator, description, timeout=0.5)
        except _UiaAmbiguousError:
            raise
        except XFLRGuiError:
            if fallback_index is None:
                raise
            edits = self._descendants(parent, control_type="Edit")
            if len(edits) > fallback_index:
                return edits[fallback_index]
            raise XFLRGuiError(
                f"Could not identify {description}; UIA exposed {len(edits)} edit control(s)."
            )

    def _toggle_sequence(self) -> None:
        try:
            control = self._control(
                self._main,
                _LOCATORS["sequence_toggle"],
                "sequence-analysis toggle",
                timeout=3.0,
            )
        except _UiaAmbiguousError:
            raise
        except XFLRGuiError:
            control = self._control(
                self._main,
                UiaLocator("Button", titles=_LOCATORS["sequence_toggle"].titles),
                "sequence-analysis toggle",
                timeout=3.0,
            )
        state: Any = None
        for method_name in ("get_toggle_state", "get_check_state"):
            method = getattr(control, method_name, None)
            if callable(method):
                state = method()
                break
        if state in (1, True, "1", "on", "On", "checked", "Checked"):
            self.sequence_on = True
            return
        toggle = getattr(control, "toggle", None)
        if not callable(toggle):
            raise XFLRGuiError("The sequence-analysis control does not expose the UIA Toggle pattern.")
        toggle()
        self.sequence_on = True

    def dump_uia_tree(self, parent: Any | None = None, max_depth: int = 8) -> list[dict[str, Any]]:
        """Return an inspectable semantic UIA tree for locator configuration.

        The result contains no screenshots or coordinates. It is intended for
        one-time inspection on the target XFLR5 Windows build when a control's
        accessible name or AutomationId differs from the default aliases.
        """

        root = self._main if parent is None else parent
        rows: list[dict[str, Any]] = []

        def walk(control: Any, depth: int) -> None:
            info = getattr(control, "element_info", None)
            rows.append(
                {
                    "depth": depth,
                    "name": self._control_name(control),
                    "control_type": getattr(info, "control_type", None),
                    "automation_id": getattr(info, "automation_id", None),
                    "class_name": getattr(info, "class_name", None),
                }
            )
            if depth >= max_depth:
                return
            try:
                children = list(control.children())
            except Exception:
                children = []
            for child in children:
                walk(child, depth + 1)

        walk(root, 0)
        return rows

    # ---------- XFLR5 workflow ----------

    def open_project_file(self, filepath: str | Path) -> None:
        self._invoke_menu_path(("File", "Open"))
        self._complete_file_dialog(filepath, "open")

    def enter_filename(self, filename: str | Path) -> None:
        dialog = self._wait_secondary_window("file dialog")
        filename_control = self._control(dialog, _LOCATORS["file_name_edit"], "file-name field")
        self._set_text(filename_control, str(Path(filename)), "file name")
        self._send_keys("{ENTER}", dialog)

    def _plane_tree(self) -> Any:
        return self._control(self._main, UiaLocator("Tree"), "XFLR5 object tree")

    @staticmethod
    def _control_name(control: Any) -> str:
        try:
            value = control.window_text()
            if value:
                return str(value).strip()
        except Exception:
            pass
        info = getattr(control, "element_info", None)
        return str(getattr(info, "name", "") or "").strip()

    def _plane_items(self) -> list[Any]:
        tree = self._plane_tree()
        try:
            items = list(tree.children(control_type="TreeItem"))
        except Exception:
            items = []
        if not items:
            items = self._descendants(tree, control_type="TreeItem")
            direct_items: list[Any] = []
            tree_key = self._element_key(tree)
            for item in items:
                try:
                    parent = item.parent()
                except Exception:
                    continue
                if self._element_key(parent) == tree_key:
                    direct_items.append(item)
            if direct_items:
                items = direct_items
        return items

    def _select_plane(self, plane: WindowsPlaneRef) -> Any:
        items = self._plane_items()
        if plane.index < 0:
            raise XFLRGuiError(f"Plane index must be non-negative, got {plane.index}.")
        if plane.index < len(items) and self._control_name(items[plane.index]) == plane.name:
            item = items[plane.index]
        else:
            matches = [item for item in items if self._control_name(item) == plane.name]
            if len(matches) != 1:
                raise XFLRGuiError(
                    f"Could not uniquely re-resolve plane {plane.name!r} at index {plane.index}; "
                    f"found {len(matches)} exact match(es)."
                )
            item = matches[0]
        selector = getattr(item, "select", None)
        if callable(selector):
            selector()
        else:
            item.set_focus()
        self._last_plane = plane
        return item

    def plane_coordinates_from_index(self, index: int) -> WindowsPlaneRef:
        items = self._plane_items()
        if index < 0 or index >= len(items):
            raise XFLRGuiError(f"Plane index {index} is outside the UIA tree, which contains {len(items)} plane(s).")
        name = self._control_name(items[index])
        if not name:
            raise XFLRGuiError(f"Plane index {index} has no accessible UIA name.")
        reference = WindowsPlaneRef(index, name)
        self._select_plane(reference)
        return reference

    def reset_to_object_level(self) -> None:
        self._plane_tree().set_focus()

    def reset_to_plane_level(self) -> None:
        reference = self._last_plane or getattr(self, "plane_location", None)
        if isinstance(reference, WindowsPlaneRef):
            self._select_plane(reference)
        else:
            self._plane_tree().set_focus()

    @staticmethod
    def key_from_part(part: str) -> str:
        keys = {"wing": "w", "rudder": "f", "elevator": "e", "body": "b"}
        try:
            return keys[part.strip().lower()]
        except KeyError as error:
            raise ValueError(f"Unsupported XFLR part: {part!r}") from error

    def _open_component_dialog(self, plane: WindowsPlaneRef, part: str) -> Any:
        self._select_plane(plane)
        self._send_keys(f"^{self.key_from_part(part)}", self._main)
        dialog = self._wait_secondary_window("component dialog")
        other = self._control(dialog, _LOCATORS["other_button"], "component Other button")
        self._invoke(other, "component Other button")
        return dialog

    def export_wing(self, plane: WindowsPlaneRef, filepath: str | Path, part: str) -> None:
        self._open_component_dialog(plane, part)
        self._complete_file_dialog(filepath, "save")
        self._wait_for(lambda: Path(filepath).is_file() or None, f"exported component {filepath}")

    def import_wing(self, plane: WindowsPlaneRef, filepath: str | Path, part: str) -> None:
        self._open_component_dialog(plane, part)
        self._complete_file_dialog(filepath, "open")

    def replace_part_from_xml(self, plane: WindowsPlaneRef, filepath: str | Path, part: str) -> None:
        self.import_wing(plane, filepath, part)
        try:
            confirmation = self._control_any_window(_LOCATORS["yes_button"], "component replacement confirmation", 2.0)
        except XFLRGuiError:
            confirmation = None
        if confirmation is not None:
            self._invoke(confirmation, "component replacement confirmation")
        else:
            self._send_keys("{ENTER}", self._main)

    def swap_part_airfoil(
        self,
        plane: WindowsPlaneRef,
        part: str,
        left_airfoil: str,
        right_airfoil: str | None = None,
        xml_path: str | Path = "component.xml",
        sections: tuple[int, int | None] = (1, None),
    ) -> None:
        self.export_wing(plane, xml_path, part)
        PlaneXmlEditor.replace_airfoils(xml_path, left_airfoil, right_airfoil, sections)
        self.replace_part_from_xml(plane, xml_path, part)

    def _configure_analysis_dialog(self, name: str, speed: float, fixed_angle: float) -> Any:
        self._send_keys("{F6}", self._main)
        dialog = self._wait_secondary_window("analysis settings dialog")
        speed_control = self._analysis_edit(dialog, "speed_edit", "analysis speed", 0)
        angle_control = self._analysis_edit(dialog, "fixed_angle_edit", "fixed angle", 1)
        name_control = self._analysis_edit(dialog, "analysis_name_edit", "analysis name", 2)
        self._set_text(speed_control, str(speed), "analysis speed")
        self._set_text(angle_control, str(fixed_angle), "fixed angle")
        self._set_text(name_control, name, "analysis name")
        try:
            ok = self._control(dialog, _LOCATORS["ok_button"], "analysis settings confirmation", timeout=1.0)
        except XFLRGuiError:
            ok = None
        if ok is not None:
            self._invoke(ok, "analysis settings confirmation")
        else:
            self._send_keys("{ENTER}", dialog)
        return dialog

    def _sweep_edits(self) -> tuple[Any, Any, Any]:
        start_button = self._control(self._main, _LOCATORS["start_analysis"], "start-analysis button")
        try:
            panel = start_button.parent()
        except Exception:
            panel = self._main
        start = self._analysis_edit(panel, "sweep_start_edit", "sweep start", 0)
        end = self._analysis_edit(panel, "sweep_end_edit", "sweep end", 1)
        increment = self._analysis_edit(panel, "sweep_increment_edit", "sweep increment", 2)
        return start, end, increment

    def _analysis_items(self, plane: WindowsPlaneRef) -> list[Any]:
        plane_item = self._select_plane(plane)
        items = self._descendants(plane_item, control_type="TreeItem")
        if not items:
            items = self._descendants(plane_item, control_type="ListItem")
        return items

    def find_analysis(
        self,
        name: str,
        plane: WindowsPlaneRef,
        max_rows: int = 200,
    ) -> Any | None:
        matches = [item for item in self._analysis_items(plane)[:max_rows] if self._control_name(item) == name]
        if len(matches) > 1:
            raise XFLRGuiError(f"Analysis name {name!r} is ambiguous in the XFLR5 tree.")
        return matches[0] if matches else None

    def _wait_for_analysis(self, name: str, plane: WindowsPlaneRef, timeout: float) -> Any:
        return self._wait_for(lambda: self.find_analysis(name, plane), f"analysis {name!r}", timeout)

    def _invoke_context_item(self, locator: UiaLocator, description: str) -> None:
        self._send_keys("+{F10}", self._main)
        item = self._control_any_window(locator, description)
        self._invoke(item, description)

    def alpha_or_beta_sweep(
        self,
        plane: WindowsPlaneRef,
        name: str,
        results_out: str | Path,
        speed: float = 70.0,
        sweep_start: float = -13.0,
        sweep_end: float = 13.0,
        sweep_increment: float = 0.5,
        fixed_angle: float = 0.0,
        wait_seconds: float = 20.0,
    ) -> None:
        self._select_plane(plane)
        self._configure_analysis_dialog(name, speed, fixed_angle)
        self._toggle_sequence()
        start, end, increment = self._sweep_edits()
        self._set_text(start, str(sweep_start), "sweep start")
        self._set_text(end, str(sweep_end), "sweep end")
        self._set_text(increment, str(sweep_increment), "sweep increment")
        start_button = self._control(self._main, _LOCATORS["start_analysis"], "start-analysis button")
        self._invoke(start_button, "start-analysis button")
        analysis_item = self._wait_for_analysis(name, plane, wait_seconds)
        selector = getattr(analysis_item, "select", None)
        if callable(selector):
            selector()
        else:
            analysis_item.set_focus()
        self._invoke_context_item(_LOCATORS["export_results"], "export-results menu item")
        self._complete_file_dialog(results_out, "save")
        self._wait_for(lambda: Path(results_out).is_file() or None, f"analysis result {results_out}", wait_seconds)
        self.reset_to_plane_level()

    def check_plane_name(self, plane: WindowsPlaneRef) -> str:
        return plane.name

    def delete_polars(self, plane: WindowsPlaneRef) -> None:
        self._select_plane(plane)
        self._invoke_context_item(_LOCATORS["delete_polars"], "delete-polars menu item")
        try:
            confirmation = self._control_any_window(_LOCATORS["yes_button"], "delete-polars confirmation", 5.0)
        except XFLRGuiError:
            confirmation = None
        if confirmation is not None:
            self._invoke(confirmation, "delete-polars confirmation")


__all__ = ["UiaLocator", "WindowsPlaneRef", "WindowsXFLRGui"]
