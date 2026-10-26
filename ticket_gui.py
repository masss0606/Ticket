# -*- coding: utf-8 -*-
"""
wesais 抢票助手 GUI (ticket_gui.py)

  python ticket_gui.py

功能:
  - Token 状态卡: 有效/失效/年龄/来源, 一键刷新(纯接口优先, UI 自动化兜底)
  - 目标场次卡: 下一个0点日期/星期/product_id/SKU, 0点倒计时
  - 操作: 检测 Token / 刷新 Token / 测试查询 / 开始抢票 / 停止
  - 实时日志终端(关键字着色), 底层复用 grab_ticket.py / ui_refresh.py
"""
import os
import sys
from runtime_log import setup_logging, log_gui_message

if __name__ == "__main__":
    setup_logging()

import subprocess
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QProcess
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QPlainTextEdit, QFrame, QLineEdit,
)

TZ = timezone(timedelta(hours=8))

QSS = '''
QWidget { background: #17181d; color: #e8e9ee; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QFrame#card { background: #22232a; border-radius: 12px; border: 1px solid #2c2e37; }
QLabel#title { font-size: 17px; font-weight: 700; }
QLabel#subtitle { color: #9a9daa; font-size: 12px; }
QLabel#cardTitle { color: #9a9daa; font-size: 12px; letter-spacing: 1px; }
QLabel#big { font-size: 20px; font-weight: 700; }
QLabel#mono { font-family: Consolas; }
QPushButton { border-radius: 8px; padding: 9px 16px; border: 1px solid #3a3d49; background: #2a2c35; color: #e8e9ee; }
QPushButton:hover { background: #33363f; }
QPushButton:disabled { color: #6b6e7a; background: #232429; }
QPushButton#primary { background: #2f9e63; border: none; font-weight: 700; }
QPushButton#primary:hover { background: #37b371; }
QPushButton#danger { background: #b03a3a; border: none; font-weight: 700; }
QPushButton#danger:hover { background: #c94b4b; }
QPlainTextEdit#console { background: #121317; border: 1px solid #2c2e37; border-radius: 10px;
  font-family: Consolas; font-size: 12px; color: #c9ccd6; }
QStatusBar { color: #9a9daa; }
'''


def next_midnight_bj():
    now = datetime.now(TZ)
    tgt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                            microsecond=0)
    return tgt


class FnThread(QThread):
    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:
            self.done.emit({'error': repr(e)})


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("wesais 游泳馆抢票助手")
        self.resize(880, 640)
        self.proc = None          # QProcess 抢票/查询
        self.refresh_threads = []
        self._checking_token = False
        self._token_check_pending = False
        self._last_check_at = None
        self._last_check_detail = ""
        self._build_ui()
        self.log("[gui] 本次运行日志: %s" % setup_logging(), '#9a9daa')
        self._token_mtime = self._cur_mtime()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        QTimer.singleShot(300, self.check_token)

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(18, 14, 18, 12)
        v.setSpacing(12)

        head = QHBoxLayout()
        t = QLabel("wesais 游泳馆抢票助手")
        t.setObjectName("title")
        sub = QLabel("提前30分钟完成刷新 · 提前10分钟复检")
        sub.setObjectName("subtitle")
        sub.setAlignment(Qt.AlignBottom)
        head.addWidget(t)
        head.addSpacing(8)
        head.addWidget(sub)
        head.addStretch(1)
        v.addLayout(head)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.card_token = self._card("TOKEN 状态")
        self.token_dot = QLabel("●")
        f = self.token_dot.font(); f.setPointSize(16); self.token_dot.setFont(f)
        self.token_main = QLabel("检测中…")
        self.token_main.setObjectName("big")
        self.token_sub = QLabel("-")
        self.token_sub.setObjectName("subtitle")
        tb = QVBoxLayout(self.card_token)
        tb.addWidget(self._card_title("TOKEN 状态"))
        row = QHBoxLayout()
        row.addWidget(self.token_dot)
        row.addWidget(self.token_main, 1)
        tb.addLayout(row)
        tb.addWidget(self.token_sub)
        grid.addWidget(self.card_token, 0, 0)

        self.card_target = self._card("目标场次")
        self.target_main = QLabel("-")
        self.target_main.setObjectName("big")
        self.target_sub = QLabel("-")
        self.target_sub.setObjectName("subtitle")
        self.target_sub.setObjectName("mono")
        tb2 = QVBoxLayout(self.card_target)
        tb2.addWidget(self._card_title("目标场次 (下一个0点)"))
        tb2.addWidget(self.target_main)
        tb2.addWidget(self.target_sub)
        grid.addWidget(self.card_target, 0, 1)

        self.card_count = self._card("距开抢")
        self.count_main = QLabel("--:--:--")
        self.count_main.setObjectName("big")
        self.count_sub = QLabel("服务器时间 00:00:00 自动开抢")
        self.count_sub.setObjectName("subtitle")
        tb3 = QVBoxLayout(self.card_count)
        tb3.addWidget(self._card_title("距开抢"))
        tb3.addWidget(self.count_main)
        tb3.addWidget(self.count_sub)
        grid.addWidget(self.card_count, 0, 2)
        v.addLayout(grid)

        btns = QHBoxLayout()
        btns.addWidget(QLabel("开抢时间"))
        self.at_edit = QLineEdit()
        self.at_edit.setPlaceholderText("HH:MM 或 +秒, 空=下一个0点")
        self.at_edit.setFixedWidth(190)
        btns.addWidget(self.at_edit)
        self.btn_check = QPushButton("检测 Token")
        self.btn_refresh = QPushButton("刷新 Token")
        self.btn_test = QPushButton("测试查询")
        self.btn_run = QPushButton("开始抢票")
        self.btn_run.setObjectName("primary")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        for b in (self.btn_check, self.btn_refresh, self.btn_test,
                  self.btn_run, self.btn_stop):
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)

        self.console = QPlainTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        v.addWidget(self.console, 1)
        self.statusBar().showMessage("就绪")

        self.btn_check.clicked.connect(self.check_token)
        self.btn_refresh.clicked.connect(self.refresh_token)
        self.btn_test.clicked.connect(lambda: self.run_script(["grab_ticket.py", "test"]))
        self.btn_run.clicked.connect(self._start_run)
        self.btn_stop.clicked.connect(self.stop_proc)

    def _card(self, _):
        f = QFrame()
        f.setObjectName("card")
        f.setMinimumHeight(110)
        return f

    def _card_title(self, text):
        l = QLabel(text)
        l.setObjectName("cardTitle")
        return l

    def log(self, text, color=None, persist=True):
        if persist:
            log_gui_message(text)
        if color:
            self.console.appendHtml('<span style="color:%s">%s</span>' % (color, text))
        else:
            self.console.appendPlainText(text)

    def _start_worker(self, work, done):
        thread = FnThread(work, self)
        thread.done.connect(done)
        self.refresh_threads.append(thread)
        def cleanup():
            if thread in self.refresh_threads:
                self.refresh_threads.remove(thread)
            thread.deleteLater()
        thread.finished.connect(cleanup)
        thread.start()

    # ---------------- Token 检测 ----------------
    def check_token(self):
        if self._checking_token:
            self._token_check_pending = True
            return
        self._checking_token = True
        self.btn_check.setEnabled(False)
        self.btn_check.setText("检测中…")
        self._set_token_dot('#8ab4ff', '检测中…')
        self.log('[gui] 检测 Token: 正在读取最新凭据并联网校验…', '#8ab4ff')

        def work():
            import grab_ticket as g
            token = g.load_token()
            if not token:
                return {'ok': False, 'status': 'missing', 'has': False, 'age': None,
                        'message': '未配置 Token，请先点击刷新 Token；未发送校验请求'}
            with g.new_session(token) as session:
                result = g.inspect_token(session, token)
            result.update(has=True, age=g.token_age(token))
            return result
        self._start_worker(work, self._token_result)

    def _token_result(self, result):
        self._checking_token = False
        self.btn_check.setEnabled(True)
        self.btn_check.setText('检测 Token')
        if result.get('error'):
            result = {'ok': False, 'status': 'error', 'message': result['error']}
        status = result.get('status', 'valid' if result.get('ok') else 'invalid')
        label, color = {
            'valid': ('有效', '#34c777'),
            'invalid': ('失效', '#e05252'),
            'missing': ('未配置', '#e05252'),
            'error': ('检测失败', '#e0b13a'),
        }.get(status, ('检测失败', '#e0b13a'))
        self._set_token_dot(color, label)
        self._last_check_at = datetime.now(TZ).strftime('%H:%M:%S')
        self._last_check_detail = '%s | HTTP=%s | code=%s | %s' % (
            label, result.get('http_status', '-'), result.get('code', '-'),
            result.get('message', ''))
        self.token_sub.setToolTip(self._last_check_detail)
        self._update_token_age(result.get('age'))
        self.log('[gui] Token 检测结果 [%s]: %s' %
                 (self._last_check_at, self._last_check_detail), color)
        if self.proc is None:
            self.statusBar().showMessage('Token 检测完成: ' + label)
        if self._token_check_pending:
            self._token_check_pending = False
            QTimer.singleShot(0, self.check_token)

    def _update_token_age(self, age):
        text = '年龄 %d 分钟' % (age // 60) if age is not None else '年龄未知/无 Token'
        if self._last_check_at:
            text += ' · 检测于 ' + self._last_check_at
        self.token_sub.setText(text)

    def _set_token_dot(self, color, text):
        self.token_dot.setStyleSheet('color: %s;' % color)
        self.token_main.setText(text)

    # ---------------- Token 刷新(纯接口 -> UI 兜底) ----------------
    def refresh_token(self):
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.log('[gui] 已有任务在运行，自动刷新由该任务管理', '#e0b13a')
            return
        self._set_task_busy(True)
        self.btn_stop.setEnabled(False)
        self.statusBar().showMessage("刷新 Token…")
        self.log("[gui] 刷新 Token: 先试 auth_code 纯接口…", '#8ab4ff')

        def work():
            import auto_ticket as a
            t = a.try_refresh_via_authcode()
            return {'pure': bool(t)}
        self._start_worker(work, self._after_pure_refresh)

    def _after_pure_refresh(self, r):
        if r.get('error'):
            self.log('[gui] 接口刷新异常: ' + r['error'], '#e0b13a')
        if r.get('pure'):
            self.log("[gui] 纯接口刷新成功", '#34c777')
            self._set_task_busy(False)
            self.statusBar().showMessage("Token 已刷新")
            self.check_token()
            return
        self.log("[gui] auth_code 不可用, 启动 UI 自动化(微信界面点击)…", '#8ab4ff')
        self.run_script(['ui_refresh.py'], after=self._after_ui_refresh)

    def _after_ui_refresh(self, code):
        self.btn_refresh.setEnabled(True)
        if code == 0:
            self.log("[gui] UI 自动化成功", '#34c777')
        else:
            self.log("[gui] UI 自动化失败 (exit=%s)，请查看上方具体原因" % code, '#e05252')
        self.statusBar().showMessage("就绪")
        self.check_token()

    # ---------------- 抢票流程 ----------------
    def _at_arg(self):
        text = self.at_edit.text().strip()
        return ['--at=' + text] if text else []

    def _start_run(self):
        args = ['grab_ticket.py', 'run', '--auto', '--refresh-min=30', '--verify-min=10']
        self.run_script(args + self._at_arg(), done_cb=self._run_finished)

    def _run_finished(self, code):
        color = '#34c777' if code == 0 else '#e05252'
        self.log('[gui] 任务结束 (exit=%s)' % code, color)
        self.check_token()

    def _set_task_busy(self, busy):
        for button in (self.btn_run, self.btn_refresh, self.btn_test):
            button.setEnabled(not busy)
        self.at_edit.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)

    # ---------------- QProcess 封装 ----------------
    def run_script(self, args, after=None, done_cb=None):
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.log("[gui] 已有任务在运行, 请先停止", '#e0b13a')
            return False
        proc = QProcess(self)
        self.proc = proc
        proc.setWorkingDirectory(BASE)
        proc.readyReadStandardOutput.connect(self._proc_out)
        proc.readyReadStandardError.connect(self._proc_err)
        completed = False
        def on_done(code, _status):
            nonlocal completed
            if completed:
                return
            completed = True
            self._proc_out()
            self._proc_err()
            self.proc = None
            self._set_task_busy(False)
            self.statusBar().showMessage('就绪')
            if done_cb:
                done_cb(code)
            if after:
                after(code)
            proc.deleteLater()
        def on_error(error):
            self.log('[gui] 子进程错误: ' + proc.errorString(), '#e05252')
            if error == QProcess.FailedToStart:
                on_done(-1, QProcess.CrashExit)
        proc.finished.connect(on_done)
        proc.errorOccurred.connect(on_error)
        self._set_task_busy(True)
        self.log("[gui] $ python " + " ".join(args), '#9a9daa')
        self.statusBar().showMessage("运行中: " + " ".join(args))
        proc.start(sys.executable, ['-u', '-X', 'utf8'] + args)
        return True

    def _proc_out(self):
        if not self.proc:
            return
        data = bytes(self.proc.readAllStandardOutput()).decode('utf-8', 'replace')
        for line in data.splitlines():
            color = None
            if ('成功' in line) or ('code=200' in line) or ('>>>' in line):
                color = '#34c777'
            elif ('失效' in line) or ('失败' in line) or ('售罄' in line) or ('[!]' in line):
                color = '#e05252'
            # 子进程已经通过 runtime_log 写入同一日志，这里只显示。
            self.log(line, color, persist=False)

    def _proc_err(self):
        if not self.proc:
            return
        data = bytes(self.proc.readAllStandardError()).decode('utf-8', 'replace')
        for line in data.splitlines():
            self.log(line, '#e0b13a', persist=False)

    def stop_proc(self):
        proc = self.proc
        if proc and proc.state() != QProcess.NotRunning:
            proc.terminate()
            if not proc.waitForFinished(3000):
                proc.kill()
            self.log("[gui] 已停止", '#e0b13a')

    # ---------------- 倒计时 ----------------
    def _cur_mtime(self):
        try:
            return os.path.getmtime(os.path.join(BASE, "token.txt"))
        except OSError:
            return 0

    def _tick(self):
        # token.txt 变化监视: 任何进程刷新 token 后 1 秒内自动重检
        m = self._cur_mtime()
        if m != self._token_mtime:
            self._token_mtime = m
            if m:
                self.log("[gui] 检测到 token.txt 已更新, 重新验证…", "#8ab4ff")
                self.check_token()
        # 年龄本地实时计算(无网络)
        try:
            import grab_ticket as g
            t = g.load_token()
            age = g.token_age(t) if t else None
            if not self._checking_token:
                self._update_token_age(age)
        except Exception:
            pass
        now = datetime.now(TZ)
        at_txt = self.at_edit.text().strip() if hasattr(self, 'at_edit') else ''
        at_dt = None
        if at_txt:
            try:
                if at_txt.startswith('+'):
                    at_dt = now + timedelta(seconds=int(at_txt[1:]))
                else:
                    parts = [int(x) for x in at_txt.split(':')]
                    hh, mm = parts[0], parts[1]
                    ss = parts[2] if len(parts) > 2 else 0
                    at_dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
                    if at_dt <= now:
                        at_dt += timedelta(days=1)
            except Exception:
                at_dt = None
        if at_dt is not None:
            tgt = at_dt
            self.count_sub.setText("本地时间 %s 自动开抢" % at_dt.strftime('%H:%M:%S'))
        else:
            tgt = next_midnight_bj()
            self.count_sub.setText("服务器时间 00:00:00 自动开抢")
        day = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][tgt.weekday()]
        pid = {0: 25294, 1: 25293, 2: 25292, 3: 25291, 4: 25289, 5: 25288, 6: 25287}[tgt.weekday()]
        ds = tgt.strftime('%Y%m%d')
        self.target_main.setText("%s (%s)" % (ds, day))
        self.target_sub.setText("product_id=%d  sku=121%08d%s:1" % (pid, pid, ds))
        remain = int((tgt - now).total_seconds())
        if remain < 0:
            remain = 0
        h, m, s = remain // 3600, remain % 3600 // 60, remain % 60
        self.count_main.setText("%02d:%02d:%02d" % (h, m, s))


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
