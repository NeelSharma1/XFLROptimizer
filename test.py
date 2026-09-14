"""Import smoke test retained for compatibility with the original project."""

import unittest


class ApplicationImportTests(unittest.TestCase):
    def test_application_imports_without_optional_gui_dependencies(self):
        import Revamped_XFLRPlaneScript

        self.assertTrue(callable(Revamped_XFLRPlaneScript.main))

