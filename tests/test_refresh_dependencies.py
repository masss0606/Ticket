"""验证刷新依赖发现和启动失败提示，不操作微信或系统代理。"""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import auto_ticket
import grab_ticket
import ui_refresh


class RefreshDependencyTests(unittest.TestCase):
    def test_current_environment_is_found_without_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Scripts" / "mitmdump.exe"
            executable.parent.mkdir()
            executable.touch()
            with patch.object(grab_ticket.sys, "prefix", directory), \
                    patch.object(grab_ticket.shutil, "which") as which:
                self.assertEqual(grab_ticket.find_mitmdump(), str(executable))
                which.assert_not_called()

    def test_project_environment_is_found_from_another_python(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            executable = project / ".venv" / "Scripts" / "mitmdump.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.object(grab_ticket.sys, "prefix", str(project / "other")), \
                    patch.object(grab_ticket, "__file__", str(project / "grab_ticket.py")), \
                    patch.object(grab_ticket.shutil, "which") as which:
                self.assertEqual(grab_ticket.find_mitmdump(), str(executable))
                which.assert_not_called()

    def test_orchestrator_uses_the_shared_finder(self):
        with patch.object(grab_ticket, "find_mitmdump", return_value="environment/mitmdump.exe"):
            self.assertEqual(auto_ticket._find_mitmdump(), "environment/mitmdump.exe")

    def test_missing_dependencies_are_reported_before_any_side_effect(self):
        output = io.StringIO()
        with patch.object(auto_ticket, "_find_mitmdump", return_value=None), \
                patch.object(ui_refresh, "configure_dpi_awareness"), \
                patch.object(ui_refresh.importlib.util, "find_spec", return_value=None), \
                patch.object(grab_ticket, "ensure_hook") as ensure_hook, \
                patch.object(auto_ticket, "proxy_on") as proxy_on, \
                patch.object(ui_refresh, "find_main_window") as find_window, \
                redirect_stdout(output):
            self.assertEqual(ui_refresh.main(), 1)
            ensure_hook.assert_not_called()
            proxy_on.assert_not_called()
            find_window.assert_not_called()
        message = output.getvalue()
        for text in ("mitmproxy (mitmdump)", "pywinauto", "Pillow", "当前 Python", "pip install -r"):
            self.assertIn(text, message)


if __name__ == "__main__":
    unittest.main()
