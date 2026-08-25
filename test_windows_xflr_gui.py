import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
