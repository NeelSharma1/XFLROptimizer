import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import Revamped_XFLRPlaneScript as application
from xflr5_core import XFLRGuiError
from xflr5_windows_gui import UiaLocator, WindowsPlaneRef, WindowsXFLRGui


class FakeElementInfo:
    def __init__(self, runtime_id):
        self.runtime_id = runtime_id
        self.handle = None


class FakeControl:
    def __init__(self, runtime_id, *, title="", control_type="Button", auto_id=""):
        self.element_info = FakeElementInfo(runtime_id)
        self.title = title
        self.control_type = control_type
        self.auto_id = auto_id
        self.invoked = False
        self.text = None

    def invoke(self):
        self.invoked = True

    def set_edit_text(self, value):
        self.text = value

    def window_text(self):
        return self.title


class FakeParent:
    def __init__(self, *controls):
        self.controls = list(controls)

    def descendants(self, **criteria):
        result = self.controls
        for key, expected in criteria.items():
            if key == "title":
                result = [control for control in result if control.title == expected]
            elif key == "control_type":
                result = [control for control in result if control.control_type == expected]
            elif key == "auto_id":
                result = [control for control in result if control.auto_id == expected]
        return result


class FakeWindow:
    def __init__(self, process_id, title, visible=True):
        self._process_id = process_id
        self._title = title
        self._visible = visible
        self.focused = False

    def is_visible(self):
        return self._visible

    def process_id(self):
        return self._process_id

    def window_text(self):
        return self._title

    def restore(self):
        self._visible = True

    def set_focus(self):
        self.focused = True


class FakeDesktop:
    def __init__(self, *windows):
        self._windows = list(windows)

    def windows(self):
        return self._windows


class WindowsUiAutomationTests(unittest.TestCase):
    def test_locator_prefers_exact_automation_id(self):
        by_id = FakeControl("id", title="Different", auto_id="startAnalysis")
        by_title = FakeControl("title", title="Start Analysis")
        parent = FakeParent(by_id, by_title)

        found = WindowsXFLRGui._locate_once(
            parent,
            UiaLocator("Button", titles=("Start Analysis",), auto_ids=("startAnalysis",)),
        )

        self.assertIs(found, by_id)

    def test_ambiguous_exact_locator_fails(self):
        parent = FakeParent(
            FakeControl("one", title="Analyze"),
            FakeControl("two", title="Analyze"),
        )

        with self.assertRaises(XFLRGuiError):
            WindowsXFLRGui._locate_once(parent, UiaLocator("Button", titles=("Analyze",)))

    def test_button_action_uses_invoke_pattern(self):
        control = FakeControl("button", title="Analyze")

        WindowsXFLRGui._invoke(control, "Analyze button")

        self.assertTrue(control.invoked)

    def test_legacy_plane_coordinates_are_replaced_by_a_named_reference(self):
        reference = WindowsPlaneRef(index=2, name="Test Plane")

        self.assertEqual(reference.index, 2)
        self.assertEqual(reference.name, "Test Plane")

    def test_cli_backend_arguments_have_deterministic_defaults(self):
        args = application._parse_cli_args([])

        self.assertEqual(args.gui_backend, "auto")
        self.assertIsNone(args.xflr5_executable)

    def test_windows_backend_is_selected_without_constructing_the_legacy_adapter(self):
        sentinel = object()
        with patch.object(application.sys, "platform", "win32"), patch(
            "xflr5_windows_gui.WindowsXFLRGui", return_value=sentinel
        ) as constructor:
            result = application._make_gui(Path("project.xfl"), "auto", Path("xflr5.exe"))

        self.assertIs(result, sentinel)
        constructor.assert_called_once_with(Path("project.xfl"), executable_path=Path("xflr5.exe"))

    def test_existing_xflr5_is_found_by_process_name_without_an_executable_path(self):
        main_window = FakeWindow(1234, "Project")
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._desktop = FakeDesktop(main_window)

        with patch("xflr5_windows_gui._windows_process_name", return_value="xflr5.exe"):
            result = adapter._find_existing_xflr5()

        self.assertEqual(result, (1234, main_window, "Project"))

    def test_actual_xflr5_process_can_have_an_arbitrary_window_title(self):
        main_window = FakeWindow(1234, "XFLR5 - Project")
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._desktop = FakeDesktop(main_window)

        with patch("xflr5_windows_gui._windows_process_name", return_value="xflr5.exe"):
            result = adapter._find_existing_xflr5()

        self.assertEqual(result, (1234, main_window, "XFLR5 - Project"))

    def test_terminal_host_with_an_xflr5_title_is_ignored(self):
        terminal_window = FakeWindow(4321, "xflr5.exe")
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._desktop = FakeDesktop(terminal_window)

        with patch("xflr5_windows_gui._windows_process_name", return_value="conhost.exe"):
            result = adapter._find_existing_xflr5()

        self.assertIsNone(result)

    def test_real_xflr5_process_is_selected_next_to_terminal_host(self):
        terminal_window = FakeWindow(4321, "xflr5.exe")
        xflr5_window = FakeWindow(1234, "Project")
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._desktop = FakeDesktop(terminal_window, xflr5_window)

        def process_name(process_id):
            return {4321: "WindowsTerminal.exe", 1234: "xflr5.exe"}[process_id]

        with patch("xflr5_windows_gui._windows_process_name", side_effect=process_name):
            result = adapter._find_existing_xflr5()

        self.assertEqual(result, (1234, xflr5_window, "Project"))

    def test_multiple_running_xflr5_processes_are_rejected(self):
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._desktop = FakeDesktop(
            FakeWindow(1234, "XFLR5 - One"),
            FakeWindow(5678, "XFLR5 - Two"),
        )

        with patch("xflr5_windows_gui._windows_process_name", return_value="xflr5.exe"):
            with self.assertRaisesRegex(XFLRGuiError, "Multiple XFLR5 processes"):
                adapter._find_existing_xflr5()

    def test_connect_attaches_to_existing_pid_without_starting_or_using_a_path(self):
        main_window = FakeWindow(1234, "XFLR5 - Project")
        app = Mock()
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)

        with patch("xflr5_windows_gui.Desktop", return_value=FakeDesktop(main_window)), patch(
            "xflr5_windows_gui.Application", return_value=app
        ), patch("xflr5_windows_gui._windows_xflr5_process_ids", return_value=(1234,)), patch(
            "xflr5_windows_gui._windows_process_name", return_value="xflr5.exe"
        ), patch("xflr5_windows_gui._windows_process_is_elevated", return_value=False):
            adapter._connect(None, timeout=5.0)

        app.connect.assert_called_once_with(process=1234, timeout=5.0)
        app.start.assert_not_called()
        self.assertIs(adapter._main, main_window)
        self.assertTrue(main_window.focused)

    def test_project_open_uses_xflr5_ctrl_o_shortcut_first(self):
        main_window = FakeWindow(1234, "Project")
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._main = main_window

        with patch.object(adapter, "_send_keys") as send_keys, patch.object(
            adapter, "_complete_file_dialog"
        ) as complete_dialog:
            adapter.open_project_file(Path("project.xfl"))

        send_keys.assert_called_once_with("^o", main_window)
        complete_dialog.assert_called_once_with(Path("project.xfl"), "open", timeout=3.0)

    def test_file_dialog_fallback_focuses_filename_and_submits_path(self):
        dialog = FakeWindow(1234, "Open")
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._main = FakeWindow(1234, "Project")
        adapter._app = Mock()
        adapter._app.windows.return_value = []
        adapter._desktop = FakeDesktop(dialog)
        adapter._process_id = 1234

        with patch.object(adapter, "_file_dialog", side_effect=XFLRGuiError("UIA controls unavailable")), patch.object(
            adapter, "_send_keys"
        ) as send_keys:
            adapter._complete_file_dialog(Path("C:/work/project.xfl"), "open", timeout=0.1)

        self.assertEqual(
            send_keys.call_args_list,
            [
                call("%n", dialog),
                call("^a"),
                call("C:/work/project.xfl"),
                call("{ENTER}"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
