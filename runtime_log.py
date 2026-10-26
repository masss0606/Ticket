# -*- coding: utf-8 -*-
"""将控制台输出同步写入一次运行共用的 UTF-8 日志。"""
import os
import sys
import threading
from datetime import datetime
from pathlib import Path


LOG_ENV = "TICKET_SESSION_LOG"
_session = None


class SessionLog:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a", encoding="utf-8", errors="replace")
        self.lock = threading.RLock()
        self.source = Path(sys.argv[0]).name
        self.error_stream = sys.stderr
        self.failed = False

    def write(self, text, channel, line_start=True):
        """每次 write 都落盘，包括未换行的进度输出。"""
        with self.lock:
            if self.failed:
                return line_start
            parts = []
            for part in text.replace("\r\n", "\n").replace("\r", "\n").splitlines(True):
                if line_start:
                    stamp = datetime.now().isoformat(sep=" ", timespec="milliseconds")
                    parts.append("[%s] [%s:%s] [%s] " %
                                 (stamp, self.source, os.getpid(), channel))
                parts.append(part)
                line_start = part.endswith("\n")
            try:
                self.file.write("".join(parts))
                self.file.flush()
            except OSError as error:
                # 磁盘故障不应中断已经等待中的抢票任务。
                self.failed = True
                if self.error_stream is not None:
                    self.error_stream.write("[日志] 写入失败: %s\n" % error)
                    self.error_stream.flush()
            return line_start


class TeeStream:
    def __init__(self, original, session, channel):
        self.original = original
        self.session = session
        self.channel = channel
        self.line_start = True

    def write(self, text):
        with self.session.lock:
            self.line_start = self.session.write(text, self.channel, self.line_start)
            if self.original is not None:
                self.original.write(text)
                self.original.flush()
        return len(text)

    def flush(self):
        if self.original is not None:
            self.original.flush()

    def isatty(self):
        return self.original is not None and self.original.isatty()

    @property
    def encoding(self):
        return getattr(self.original, "encoding", "utf-8")

    def reconfigure(self, **kwargs):
        if self.original is not None and hasattr(self.original, "reconfigure"):
            self.original.reconfigure(**kwargs)

    def __getattr__(self, name):
        return getattr(self.original, name)


def setup_logging():
    """仅在入口调用；子进程继承日志路径，导入业务模块不会另建日志。"""
    global _session
    if _session is not None:
        return _session.path
    inherited = os.environ.get(LOG_ENV)
    if inherited:
        path = Path(inherited)
    else:
        name = "%s_%s_%s.log" % (
            Path(sys.argv[0]).stem,
            datetime.now().strftime("%Y%m%d_%H%M%S_%f"), os.getpid())
        path = Path(__file__).resolve().parent / "logs" / name
    _session = SessionLog(path)
    os.environ[LOG_ENV] = str(_session.path)
    sys.stdout = TeeStream(sys.stdout, _session, "stdout")
    sys.stderr = TeeStream(sys.stderr, _session, "stderr")
    print("[日志] 本次运行日志: %s" % _session.path)
    return _session.path


def log_gui_message(text):
    """GUI 自身的消息不经过 stdout，需要显式写入。"""
    if _session is not None:
        _session.write(text + "\n", "gui")
