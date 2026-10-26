"""离线验证日志，不连接微信或业务接口。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BASE = Path(__file__).resolve().parents[1]


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "session.log"
        self.env = dict(os.environ, TICKET_SESSION_LOG=str(self.log),
                        PYTHONPATH=str(BASE), PYTHONIOENCODING="utf-8")

    def run_code(self, code):
        return subprocess.run([sys.executable, "-c", code], cwd=BASE,
                              env=self.env, capture_output=True,
                              encoding="utf-8", timeout=20)

    def test_stdout_stderr_child_and_immediate_flush(self):
        result = self.run_code('''
import os, sys, subprocess
from pathlib import Path
from runtime_log import setup_logging
path = setup_logging()
assert setup_logging() == path
print("中文进度", end="")
assert "中文进度" in path.read_text(encoding="utf-8")
print("\\r下一步")
print("错误提示", file=sys.stderr)
subprocess.run([sys.executable, "-c",
    "from runtime_log import setup_logging; setup_logging(); print('子进程输出')"],
    check=True)
print("父进程完成")
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("中文进度", result.stdout)
        self.assertIn("错误提示", result.stderr)
        content = self.log.read_text(encoding="utf-8")
        for message in ("中文进度", "下一步", "错误提示", "子进程输出", "父进程完成"):
            self.assertEqual(content.count(message), 1, content)
        self.assertIn("[stderr]", content)

    def test_pythonw_streams_and_uncaught_exception(self):
        result = self.run_code('''
import sys
sys.stdout = sys.stderr = None
from runtime_log import setup_logging, log_gui_message
setup_logging()
sys.stdout.reconfigure(encoding="utf-8")
print("后台刷新输出")
log_gui_message("界面状态消息")
raise RuntimeError("异常也要保存")
''')
        self.assertNotEqual(result.returncode, 0)
        content = self.log.read_text(encoding="utf-8")
        for message in ("后台刷新输出", "界面状态消息", "Traceback", "异常也要保存"):
            self.assertIn(message, content)

    def test_cli_entrypoints_without_business_requests(self):
        for args in (["grab_ticket.py"], ["capture_payment.py", "--help"]):
            result = subprocess.run([sys.executable, *args], cwd=BASE,
                                    env=self.env, capture_output=True,
                                    encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("本次运行日志", result.stdout)
        content = self.log.read_text(encoding="utf-8")
        self.assertIn("grab_ticket.py:", content)
        self.assertIn("capture_payment.py:", content)

    def test_gui_and_child_share_log_without_duplicates(self):
        self.env["QT_QPA_PLATFORM"] = "offscreen"
        result = self.run_code('''
from unittest.mock import patch
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from runtime_log import setup_logging
from ticket_gui import MainWindow
setup_logging()
app = QApplication([])
with patch.object(MainWindow, "check_token", lambda self: None):
    window = MainWindow()
    loop = QEventLoop()
    window.log("界面测试消息")
    window.run_script(["-c",
        "from runtime_log import setup_logging; setup_logging(); print('唯一子进程消息')"],
        done_cb=lambda code: loop.quit())
    QTimer.singleShot(10000, loop.quit)
    loop.exec()
    assert "唯一子进程消息" in window.console.toPlainText()
    window.close()
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.log.read_text(encoding="utf-8")
        self.assertIn("[gui] 界面测试消息", content)
        # GUI 的命令回显也包含这段文本，只统计真正的 stdout 行。
        self.assertEqual(content.count("[stdout] 唯一子进程消息"), 1, content)

    def test_gui_refresh_failure_preserves_child_diagnostic_and_exit_code(self):
        self.env["QT_QPA_PLATFORM"] = "offscreen"
        result = self.run_code('''
from unittest.mock import patch
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from runtime_log import setup_logging
from ticket_gui import MainWindow
setup_logging()
app = QApplication([])
with patch.object(MainWindow, "check_token", lambda self: None):
    window = MainWindow()
    loop = QEventLoop()
    completed = []
    def finished(code):
        completed.append(code)
        window._after_ui_refresh(code)
        loop.quit()
    window.run_script(["-c",
        "import sys; print('缺少刷新依赖: mitmproxy'); sys.exit(7)"], after=finished)
    QTimer.singleShot(10000, loop.quit)
    loop.exec()
    assert completed == [7], completed
    text = window.console.toPlainText()
    assert "缺少刷新依赖: mitmproxy" in text, text
    assert "UI 自动化失败 (exit=7)" in text, text
    assert window.btn_refresh.isEnabled()
    window.close()
''')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
