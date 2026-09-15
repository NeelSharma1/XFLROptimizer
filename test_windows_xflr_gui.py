import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock, call, patch

import Revamped_XFLRPlaneScript as application
from xflr5_core import XFLRGuiError
from xflr5_windows_gui import NoPatternInterfaceError, UiaLocator, WindowsPlaneRef, WindowsXFLRGui, _LOCATORS


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
        self.focused = False
        self.enabled = True

    def invoke(self):
        self.invoked = True

    def set_edit_text(self, value):
        self.text = value

    def window_text(self):
        return self.title

    def set_focus(self):
        self.focused = True

    def is_enabled(self):
        return self.enabled


class SelectionPatternMissingControl(FakeControl):
    def select(self):
        raise NoPatternInterfaceError()

    def set_focus(self):
        self.focused = True


class InvokePatternMissingControl(FakeControl):
    def invoke(self):
        raise NoPatternInterfaceError()


class EditPatternMissingControl(FakeControl):
    def set_edit_text(self, _value):
        raise NoPatternInterfaceError()


class TogglePatternMissingControl(FakeControl):
    def get_toggle_state(self):
        raise NoPatternInterfaceError()

    def toggle(self):
        raise NoPatternInterfaceError()


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

    def is_enabled(self):
        return True


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

    def test_button_action_falls_back_to_enter_when_invoke_pattern_is_missing(self):
        control = InvokePatternMissingControl("button", title="Analyze")

        with patch("xflr5_windows_gui.send_keys") as send_keys:
            WindowsXFLRGui._invoke(control, "Analyze button")

        self.assertTrue(control.focused)
        send_keys.assert_called_once_with(
            "{ENTER}",
            with_spaces=True,
            with_tabs=True,
            with_newlines=True,
            vk_packet=False,
        )

    def test_text_entry_falls_back_to_keyboard_when_value_pattern_is_missing(self):
        control = EditPatternMissingControl("edit", title="Speed", control_type="Edit")

        with patch("xflr5_windows_gui.send_keys") as send_keys:
            WindowsXFLRGui._set_text(control, "70", "speed")

        self.assertTrue(control.focused)
        expected_kwargs = {
            "with_spaces": True,
            "with_tabs": True,
            "with_newlines": True,
            "vk_packet": False,
        }
        self.assertEqual(
            send_keys.call_args_list,
            [call("^a", **expected_kwargs), call("70", **expected_kwargs)],
        )

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
        adapter._startup_timeout = 1.0

        with patch.object(adapter, "_send_keys") as send_keys, patch.object(
            adapter, "_complete_file_dialog"
        ) as complete_dialog:
            adapter.open_project_file(Path("project.xfl"))

        send_keys.assert_called_once_with("^o", main_window)
        complete_dialog.assert_called_once_with(Path("project.xfl"), "open")

    def test_file_dialog_waits_then_types_into_assumed_focused_filename_field(self):
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)

        with patch("xflr5_windows_gui.time.sleep") as sleep, patch.object(adapter, "_send_keys") as send_keys:
            adapter._complete_file_dialog(Path("C:/work/project.xfl"), "open")

        sleep.assert_called_once_with(0.1)
        self.assertEqual(
            send_keys.call_args_list,
            [call("C:/work/project.xfl"), call("{ENTER}")],
        )

    def test_file_dialog_prefers_the_native_filename_control_when_available(self):
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        filename = FakeControl("1148", title="File name:", control_type="Edit")

        with patch("xflr5_windows_gui.time.sleep"), patch.object(
            adapter, "_control_any_window", return_value=filename
        ), patch.object(adapter, "_send_keys") as send_keys:
            adapter._complete_file_dialog(Path("C:/work/project.xfl"), "open")

        self.assertEqual(filename.text, "C:/work/project.xfl")
        send_keys.assert_called_once_with("{ENTER}", filename)

    def test_plane_selection_falls_back_to_focus_when_selection_pattern_is_missing(self):
        item = SelectionPatternMissingControl("plane", title="Plane 1", control_type="TreeItem")
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._plane_items = Mock(return_value=[item])

        result = adapter._select_plane(WindowsPlaneRef(0, "Plane 1"))

        self.assertIs(result, item)
        self.assertTrue(item.focused)
        self.assertEqual(adapter._last_plane, WindowsPlaneRef(0, "Plane 1"))

    def test_sequence_toggle_falls_back_to_space_when_toggle_pattern_is_missing(self):
        control = TogglePatternMissingControl("toggle", title="Sequence", control_type="CheckBox")
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._main = FakeWindow(1234, "Project")
        adapter._control = Mock(return_value=control)

        with patch.object(adapter, "_send_keys") as send_keys:
            adapter._toggle_sequence()

        send_keys.assert_called_once_with("{SPACE}", control)
        self.assertTrue(adapter.sequence_on)

    def test_analysis_completion_waits_for_analyze_button_to_be_reenabled(self):
        button = FakeControl("analyze", title="Analyze")
        button.is_enabled = Mock(side_effect=[False, False, True])
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._startup_timeout = 1.0

        with patch("xflr5_windows_gui.time.sleep"):
            adapter._wait_for_analysis_finished(button, 1.0)

        self.assertEqual(button.is_enabled.call_count, 3)

    def test_beta_dialog_selects_type_five_and_sets_fixed_alpha(self):
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._main = FakeWindow(1234, "Project")
        dialog = object()
        name = FakeControl("name", control_type="Edit")
        speed = FakeControl("speed", control_type="Edit")
        angle = FakeControl("alpha", control_type="Edit")
        save = FakeControl("save", title="Save")

        def locate(_parent, _locator, description, timeout=10.0):
            if "automatic" in description:
                raise XFLRGuiError("not exposed")
            return save

        with patch.object(adapter, "_send_keys") as send_keys, patch.object(
            adapter, "_wait_secondary_window", return_value=dialog
        ), patch.object(adapter, "_select_radio") as select_radio, patch.object(
            adapter, "_analysis_edit", side_effect=[name, speed, angle]
        ) as analysis_edit, patch.object(adapter, "_control", side_effect=locate):
            adapter._configure_analysis_dialog("beta-run", 70.0, 2.0, "beta")

        send_keys.assert_called_once_with("{VK_F6}", adapter._main)
        select_radio.assert_called_once_with(dialog, _LOCATORS["type_5_radio"], "beta-sweep polar type")
        self.assertEqual(analysis_edit.call_args_list[2].args[1], "alpha_edit")
        self.assertEqual((name.text, speed.text, angle.text), ("beta-run", "70.0", "2.0"))
        self.assertTrue(save.invoked)

    def test_stability_control_point_parser_accepts_xflr_bare_tree_names(self):
        self.assertEqual(WindowsXFLRGui._control_value_from_name("  -1.250 "), -1.25)
        self.assertEqual(WindowsXFLRGui._control_value_from_name("Control value = 0.500"), 0.5)
        self.assertIsNone(WindowsXFLRGui._control_value_from_name("Mode 1"))

    def test_stability_export_selects_control_point_nearest_zero(self):
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._main = FakeWindow(1234, "Project")
        plane = WindowsPlaneRef(0, "Plane 1")
        start_button = FakeControl("start", title="Analyze")
        edits = tuple(FakeControl(str(index), control_type="Edit") for index in range(3))
        points = [
            FakeControl("minus", title=" -1.000 ", control_type="TreeItem"),
            FakeControl("mode", title="Mode 1", control_type="TreeItem"),
            FakeControl("zero", title="  0.000 ", control_type="TreeItem"),
            FakeControl("plus", title="  1.000 ", control_type="TreeItem"),
        ]

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "modal.txt"

            def save_file(path, _mode):
                Path(path).write_text("modal", encoding="utf-8")

            with patch.object(adapter, "_select_plane"), patch.object(
                adapter, "_configure_stability_dialog"
            ), patch.object(adapter, "_toggle_sequence"), patch.object(
                adapter, "_enable_operating_point_storage"
            ), patch.object(adapter, "_sweep_edits", return_value=edits), patch.object(
                adapter, "_control", return_value=start_button
            ), patch.object(adapter, "_wait_for_analysis_finished"), patch.object(
                adapter, "_wait_for_analysis_data", return_value=(object(), points)
            ), patch.object(adapter, "_select_tree_item") as select_tree, patch.object(
                adapter, "_invoke_context_item"
            ), patch.object(adapter, "_complete_file_dialog", side_effect=save_file), patch.object(
                adapter, "_wait_for", return_value=True
            ), patch.object(adapter, "reset_to_plane_level"):
                adapter.stability_analysis(plane, "stability", destination)

        select_tree.assert_called_once_with(points[2], "stability operating point for 'stability'")

    def test_component_editor_is_found_when_other_control_is_inside_main_ui(self):
        other = FakeControl("other", title="Other...", control_type="Button")
        main_window = FakeParent(other)
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._main = main_window
        adapter._app = None
        adapter._desktop = None
        adapter._startup_timeout = 1.0

        self.assertIs(adapter._component_dialog(), main_window)

    def test_component_editor_uses_semantic_other_control_after_opening(self):
        other = FakeControl("other", title="Other...", control_type="Button")
        main_window = FakeParent(other)
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)
        adapter._main = main_window
        adapter._app = None
        adapter._desktop = None
        adapter._startup_timeout = 1.0

        with patch.object(adapter, "_select_plane"), patch.object(adapter, "_send_keys") as send_keys:
            result = adapter._open_component_dialog(WindowsPlaneRef(0, "Plane 1"), "wing", xml_tab_count=2)

        self.assertIs(result, main_window)
        self.assertEqual(
            send_keys.call_args_list,
            [call("^w", main_window), call("{ENTER}"), call("{TAB}"), call("{TAB}"), call("{ENTER}")],
        )
        self.assertTrue(other.focused)

    def test_component_xml_action_uses_the_requested_tab_count(self):
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)

        with patch.object(adapter, "_send_keys") as send_keys:
            adapter._select_component_xml_action(1)

        self.assertEqual(send_keys.call_args_list, [call("{TAB}"), call("{ENTER}")])

    def test_component_editor_is_saved_and_closed_through_uia(self):
        save = FakeControl("save", title="Save and Close", control_type="Button")
        dialog = FakeParent(save)
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)

        with patch.object(adapter, "_send_keys") as send_keys:
            adapter._finish_component_dialog(dialog)

        self.assertTrue(save.invoked)
        send_keys.assert_not_called()

    def test_export_skips_an_existing_destination(self):
        adapter = WindowsXFLRGui.__new__(WindowsXFLRGui)

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "component.xml"
            destination.write_text("already exported", encoding="utf-8")

            with patch.object(adapter, "_open_component_dialog") as open_dialog:
                adapter.export_wing(WindowsPlaneRef(0, "Plane 1"), destination, "wing")

        open_dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
