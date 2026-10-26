# -*- coding: utf-8 -*-
"""抓取一次‘去支付’的请求与响应；不创建订单、不重放或提交支付请求。"""

import argparse
from datetime import datetime
import shutil
import socket
import subprocess
import sys
if __name__ == "__main__":
    from runtime_log import setup_logging
    setup_logging()

import tempfile
import threading
import time
from pathlib import Path
import winreg

import auto_ticket as proxy


BASE = Path(__file__).resolve().parent
PROXY_HOST = "127.0.0.1"


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((PROXY_HOST, port)) == 0


def find_mitmweb():
    candidates = [
        Path(sys.prefix) / "Scripts" / "mitmweb.exe",
        Path.home() / ".conda" / "envs" / "ticket" / "Scripts" / "mitmweb.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return shutil.which("mitmweb")


def snapshot_proxy():
    state = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, proxy.INTERNET_SETTINGS) as key:
        for name in ("ProxyEnable", "ProxyServer"):
            try:
                state[name] = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                state[name] = None
    return state


def restore_proxy(state):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, proxy.INTERNET_SETTINGS,
                        0, winreg.KEY_SET_VALUE) as key:
        for name, entry in state.items():
            if entry is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                value, value_type = entry
                winreg.SetValueEx(key, name, 0, value_type, value)
    proxy._notify_proxy_change()


def build_command(executable, directory, port, web_port):
    return [executable, "--listen-host", PROXY_HOST, "--listen-port", str(port),
            "--web-host", PROXY_HOST, "--web-port", str(web_port),
            "--no-web-open-browser",
            "--allow-hosts", r"^api\.wesais\.com(?::443)?$",
            "--set", r"view_filter=~d ^api\.wesais\.com$",
            "--set", "payment_capture_dir=" + str(directory),
            "-s", str(BASE / "payment_hook.py")]


def wait_ready(proc, directory, port, web_port, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("mitmweb 启动退出(exit=%s)" % proc.returncode)
        if ((directory / "payment.mitm").exists() and port_in_use(port)
                and port_in_use(web_port)):
            return
        time.sleep(0.2)
    raise RuntimeError("mitmweb 启动超时")


def stop_capture(proc, directory):
    if proc is None or proc.poll() is not None:
        return
    (directory / "stop").touch()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=5)


def wait_for_user(proc, timeout):
    stopped = threading.Event()

    def read_enter():
        try:
            input()
        except EOFError:
            pass
        finally:
            stopped.set()

    threading.Thread(target=read_enter, daemon=True).start()
    deadline = time.monotonic() + timeout
    while not stopped.wait(0.2):
        if proc.poll() is not None:
            raise RuntimeError("mitmweb 已退出(exit=%s)" % proc.returncode)
        if time.monotonic() >= deadline:
            print("[抓包] 已到时间上限，自动停止。", flush=True)
            break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080, help="本地代理端口")
    parser.add_argument("--web-port", type=int, default=8081, help="浏览器控制台端口")
    parser.add_argument("--timeout", type=int, default=600, help="最长抓包秒数，默认 600")
    args = parser.parse_args(argv)
    if not (0 < args.port < 65536 and 0 < args.web_port < 65536
            and args.port != args.web_port and args.timeout > 0):
        parser.error("端口应为不同的 1–65535 整数，超时应为正数")
    executable = find_mitmweb()
    if not executable:
        print("[!] 未找到 mitmweb，请先在 ticket 环境安装 mitmproxy")
        return 1
    for port in (args.port, args.web_port):
        if port_in_use(port):
            print("[!] 端口 %s 已被占用，请先结束已有抓包任务。" % port)
            return 1

    captures = BASE / "captures"
    captures.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(
        prefix=datetime.now().strftime("payment-%Y%m%d-%H%M%S-"), dir=captures))
    log = directory / "mitmweb.log"
    state = snapshot_proxy()
    proc = None
    proxy_changed = False
    result = 0
    print("[抓包] 本次记录：%s" % directory, flush=True)
    try:
        with log.open("wb") as output:
            kwargs = {"cwd": BASE, "stdout": output, "stderr": subprocess.STDOUT}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(build_command(executable, directory,
                                                   args.port, args.web_port), **kwargs)
        wait_ready(proc, directory, args.port, args.web_port)
        proxy.proxy_on(args.port)
        proxy_changed = True
        print("[抓包] 已就绪，浏览器控制台：http://127.0.0.1:%d" % args.web_port, flush=True)
        print("请重新打开奥冠体育 → 我的订单 → 未支付 → 去支付。", flush=True)
        print("操作完成后返回订单列表，查看状态是否变为已支付。", flush=True)
        print("回到本窗口按 Enter 或 Ctrl+C 停止；最长 %d 秒。" % args.timeout, flush=True)
        wait_for_user(proc, args.timeout)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print("[!] %s；启动日志：%s" % (error, log), flush=True)
        result = 1
    finally:
        if proxy_changed:
            try:
                restore_proxy(state)
                print("[抓包] 已恢复原来的系统代理设置。", flush=True)
            except Exception as error:
                print("[!] 代理恢复失败：%s" % error, flush=True)
                result = 1
        try:
            stop_capture(proc, directory)
        except Exception as error:
            print("[!] 关闭 mitmweb 失败：%s" % error, flush=True)
            result = 1
    stream = directory / "payment.mitm"
    if stream.exists() and stream.stat().st_size:
        print("[抓包] 已保存请求/响应：%s" % stream, flush=True)
    else:
        print("[抓包] 未捕获到业务响应；确认小程序已重新打开并经过代理。", flush=True)
    return result


if __name__ == "__main__":
    sys.exit(main())
