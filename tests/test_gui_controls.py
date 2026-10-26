"""在离屏 Qt 界面上真实点击按钮；HTTP、Token 文件和自动刷新均替换为桩。"""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QPushButton

import grab_ticket as g
from ticket_gui import MainWindow


class GuiControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('ticket_gui.QTimer.singleShot'):
            self.window = MainWindow()
        self.window.timer.stop()
        self.window.console.clear()

    def tearDown(self):
        for thread in list(self.window.refresh_threads):
            thread.wait(2000)
        self.app.processEvents()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def wait_for_check(self):
        deadline = time.monotonic() + 3
        while (self.window._checking_token or self.window.refresh_threads) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertFalse(self.window._checking_token)
        self.assertFalse(self.window.refresh_threads)

    def test_check_button_shows_progress_and_actual_http_result(self):
        release = threading.Event()
        def response(*_args):
            release.wait(1)
            return {'ok': True, 'status': 'valid', 'http_status': 200, 'code': 200, 'message': '校验通过'}
        with patch.object(g, 'load_token', return_value='test-token'), \
                patch.object(g, 'token_age', return_value=80), \
                patch.object(g, 'inspect_token', side_effect=response) as inspect:
            self.window.btn_check.click()
            self.assertFalse(self.window.btn_check.isEnabled())
            self.assertEqual(self.window.token_main.text(), '检测中…')
            release.set()
            self.wait_for_check()
        self.assertEqual(inspect.call_count, 1)
        self.assertTrue(self.window.btn_check.isEnabled())
        self.assertEqual(self.window.token_main.text(), '有效')
        self.assertIn('HTTP=200 | code=200', self.window.console.toPlainText())
        self.assertIn('检测于', self.window.token_sub.text())

    def test_missing_token_explains_no_request_was_sent(self):
        with patch.object(g, 'load_token', return_value=None), \
                patch.object(g, 'inspect_token') as inspect:
            self.window.btn_check.click()
            self.wait_for_check()
            inspect.assert_not_called()
        self.assertEqual(self.window.token_main.text(), '未配置')
        self.assertIn('未发送校验请求', self.window.console.toPlainText())

    def test_network_failure_restores_button_and_displays_reason(self):
        with patch.object(g, 'load_token', return_value='test-token'), \
                patch.object(g, 'token_age', return_value=80), \
                patch.object(g, 'inspect_token', return_value={
                    'ok': False, 'status': 'error', 'message': '连接超时', 'http_status': None, 'code': None}):
            self.window.btn_check.click()
            self.wait_for_check()
        self.assertEqual(self.window.token_main.text(), '检测失败')
        self.assertTrue(self.window.btn_check.isEnabled())
        self.assertIn('连接超时', self.window.console.toPlainText())

    def test_removed_modes_are_absent_and_start_has_fixed_readiness_policy(self):
        labels = [button.text() for button in self.window.findChildren(QPushButton)]
        self.assertNotIn('立即演练', labels)
        self.assertNotIn('定时测试(真实请求)', labels)
        self.assertFalse(hasattr(self.window, 'btn_dry'))
        self.assertFalse(hasattr(self.window, 'btn_schedule_test'))
        self.window.at_edit.setText('14:30')
        with patch.object(self.window, 'run_script') as run:
            self.window.btn_run.click()
            args = run.call_args.args[0]
        self.assertEqual(args, ['grab_ticket.py', 'run', '--auto', '--refresh-min=30', '--verify-min=10', '--at=14:30'])


if __name__ == '__main__':
    unittest.main()
