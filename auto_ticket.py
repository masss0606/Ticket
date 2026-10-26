# -*- coding: utf-8 -*-
"""
全自动抢票编排器 v3 (auto_ticket.py)

流程(全程无需人工点击):
  1. 备份并开启系统代理 -> 127.0.0.1:8080 (广播变更)
  2. 启动 mitmdump + token_hook
     - hook_log.txt     : wesais 关键请求(auth_code/token 捕获与错误)
     - all_traffic.log  : 经代理的全部请求(诊断流量是否经过代理)
  3. 检查微信主进程(未运行则先唤起微信), 唤起 wesais 小程序
  4. 等待捕获新 token; 25s/50s 无捕获自动重新唤起(最多3次)
  5. 恢复系统代理, 停止 mitmdump
  6. 有 auth_code 先试纯接口刷新; 最后静默启动抢票

用法:
  python auto_ticket.py                  # 抓token -> 等0点抢票(全流程)
  python auto_ticket.py run --now        # 抓token -> 立即下单(演练)
  python auto_ticket.py test             # 抓token -> 只查询验证
  python auto_ticket.py --scheme-only    # 只做"代理+mitm+唤起+抓token"
  python auto_ticket.py --refresh-only   # 只用 auth_code 纯接口刷新
可选:
  --proxy-port=8080      本地代理端口
  --scheme-timeout=75    唤起后等待新 token 秒数
  --no-cold              不清理旧小程序进程(默认不清理, 避免竞态错误页)
"""

import os
import sys
if __name__ == "__main__":
    from runtime_log import setup_logging
    setup_logging()

import time
import socket
import subprocess
import winreg

BASE = os.path.dirname(os.path.abspath(__file__))
PROXY_HOST = "127.0.0.1"
PROXY_PORT_DEFAULT = 8080
SCHEME = "weixin://dl/business/?appid=wxdd06284882e39efb&env=release"
SCHEME_RETRY_AT = (25, 50)   # 在这些秒点若无捕获则重新唤起
SCHEME_TIMEOUT_DEFAULT = 75
INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
REFRESH_LOCK = os.path.join(BASE, "refresh.lock")
REFRESH_INTERVAL_MIN = 20

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                           line_buffering=True, write_through=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace",
                           line_buffering=True, write_through=True)
except Exception:
    pass


# -------------------- 代理管理 --------------------

def _notify_proxy_change():
    try:
        import ctypes
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(None, 39, None, 0)
        wininet.InternetSetOptionW(None, 37, None, 0)
    except Exception:
        pass


def _inet():
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0,
                          winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)


def proxy_enabled():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS)
        v, _ = winreg.QueryValueEx(k, "ProxyEnable")
        winreg.CloseKey(k)
        return bool(v)
    except Exception:
        return False


def proxy_server():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS)
        v, _ = winreg.QueryValueEx(k, "ProxyServer")
        winreg.CloseKey(k)
        return v or ""
    except Exception:
        return ""


def proxy_on(port):
    k = _inet()
    try:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ,
                          "%s:%d" % (PROXY_HOST, port))
    finally:
        winreg.CloseKey(k)
    _notify_proxy_change()


def proxy_off():
    k = _inet()
    try:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
    finally:
        winreg.CloseKey(k)
    _notify_proxy_change()


# -------------------- 进程与唤起 --------------------

def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    r = s.connect_ex((PROXY_HOST, port))
    s.close()
    return r == 0


def _find_mitmdump():
    # GUI 经 pythonw 启动时，当前环境的 Scripts 不一定在 PATH 中。
    from grab_ticket import find_mitmdump
    return find_mitmdump()


def _run_text(args):
    try:
        r = subprocess.run(args, capture_output=True, timeout=8)
        return (r.stdout or b"").decode("gbk", errors="replace")
    except Exception:
        return ""


def wechat_main_running():
    out = _run_text(["tasklist", "/fi", "imagename eq Weixin.exe"])
    if "Weixin.exe" in out:
        return True
    out = _run_text(["tasklist", "/fi", "imagename eq WeChat.exe"])
    return "WeChat.exe" in out


def ensure_wechat_main():
    if wechat_main_running():
        return True
    print("  微信主进程未运行, 先唤起微信…")
    try:
        os.startfile("weixin://")
        time.sleep(5)
    except Exception as e:
        print("  微信唤起失败: %s" % e)
        return False
    return wechat_main_running()


def launch_scheme(sch):
    try:
        os.startfile(sch)
        print("  已唤起: %s" % sch)
        return True
    except Exception as e:
        print("  唤起失败: %s (%s)" % (sch[:50], e))
        return False


# -------------------- auth_code 纯接口刷新 --------------------

def try_refresh_via_authcode():
    import grab_ticket as g
    code_file = os.path.join(BASE, "auth_code.txt")
    time_file = os.path.join(BASE, "auth_code_time.txt")
    if not (os.path.exists(code_file) and os.path.exists(time_file)):
        return None
    try:
        with open(time_file) as f:
            t = int(f.read().strip())
    except Exception:
        return None
    age_min = (time.time() - t) / 60.0
    if age_min > 60:
        print("  auth_code 太旧(%.0f 分钟), 跳过纯接口刷新" % age_min)
        return None
    if os.path.exists(REFRESH_LOCK):
        try:
            if time.time() - os.path.getmtime(REFRESH_LOCK) < REFRESH_INTERVAL_MIN * 60:
                print("  距上次纯刷新不足 %d 分钟(防风控), 跳过" % REFRESH_INTERVAL_MIN)
                return None
        except Exception:
            pass
    try:
        with open(code_file) as f:
            auth = f.read().strip()
    except Exception:
        return None
    s = g.new_session()
    try:
        r = s.post("https://api.wesais.com/member/wxMember/exToken",
                   data={"auth_code": auth, "business_id": g.BUSINESS_ID,
                         "request_id": g.rid()},
                    timeout=8)
        j = r.json()
        token = (j.get("data") or {}).get("token", "")
        if j.get("code") == 200 and token.startswith("eyJ"):
            g.save_token(token)
            open(REFRESH_LOCK, "w").write(str(time.time()))
            print("  auth_code 纯接口刷新成功! (auth_code 年龄 %.0f 分钟)" % age_min)
            return token
        print("  auth_code 刷新失败: code=%s %s" % (j.get("code"),
                                                   j.get("message") or j.get("msg")))
    except Exception as e:
        print("  auth_code 刷新异常: %s" % e)
    return None


# -------------------- 日志工具 --------------------

def tail_log(path, n=15):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except Exception:
        return []


# -------------------- 抓 token 主流程 --------------------

def capture_token(port, timeout, cold=False):
    import grab_ticket as g
    if not g.ensure_hook():
        return None, "hook 写入失败"
    mitm = _find_mitmdump()
    if not mitm:
        return None, "未找到 mitmdump"
    if port_in_use(port):
        return None, "端口 %d 已被占用" % port
    hook_log = os.path.join(BASE, "hook_log.txt")
    all_log = os.path.join(BASE, "all_traffic.log")
    for p in (hook_log, all_log):
        try:
            os.remove(p)
        except Exception:
            pass

    old_enabled = proxy_enabled()
    old_server = proxy_server()
    print("[1/5] 开启系统代理 -> %s:%d (原: %s %s)" %
          (PROXY_HOST, port, "开" if old_enabled else "关", old_server))
    proxy_on(port)
    proc = None
    token = None
    err = None
    try:
        print("[2/5] 启动 mitmdump…")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000
        proc = subprocess.Popen([mitm, "-s", g.HOOK_FILE, "-p", str(port), "-q"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, cwd=BASE, **kwargs)
        time.sleep(2)
        if proc.poll() is not None:
            return None, "mitmdump 启动即退出(exit=%s)" % proc.returncode

        print("[3/5] 检查微信与唤起准备…")
        if cold:
            subprocess.run(["taskkill", "/f", "/im", "WeChatAppEx.exe"],
                           capture_output=True, timeout=8)
            time.sleep(2)
            print("  冷启动: 已清理旧小程序进程")
        ensure_wechat_main()
        baseline = g.token_age() or 0

        print("[4/5] 唤起小程序并等待捕获 (%d 秒, 自动重试)…" % timeout)
        start = time.time()
        launches = 0
        while time.time() - start < timeout:
            if token is None:
                t = g.load_token()
                ct = g.token_age(t) or 0
                if t and ct < 600 and (baseline == 0 or ct < baseline):
                    token = t
                    break
            elapsed = time.time() - start
            if launches < len(SCHEME_RETRY_AT) and elapsed >= SCHEME_RETRY_AT[launches]:
                launches += 1
                print("\n  [重试 %d] 重新唤起小程序…" % launches)
                launch_scheme(SCHEME)
            print(".", end="", flush=True)
            time.sleep(1)
        print()
        if not token:
            err = "超时未捕获到新 token"
        print("[5/5] 收尾…")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if old_enabled and old_server and old_server != "%s:%d" % (PROXY_HOST, port):
            k = _inet()
            try:
                winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, old_server)
            finally:
                winreg.CloseKey(k)
            _notify_proxy_change()
            print("清理: 已恢复原代理 %s" % old_server)
        else:
            proxy_off()
            print("清理: 代理已关闭")

    if token:
        for line in tail_log(hook_log, 8):
            print("  | " + line)
    else:
        print("\n--- all_traffic.log (最后15条) ---")
        lines = tail_log(all_log)
        print("\n".join(lines) if lines else "(空: 没有任何流量经过代理!)")
        print("--- hook_log.txt ---")
        hlines = tail_log(hook_log)
        print("\n".join(hlines) if hlines else "(空)")
    return token, err


# -------------------- 入口 --------------------

def _ct(t):
    import base64, json
    try:
        p = t.split(".")[1]
        p += "=" * (4 - len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("ct", 0)
    except Exception:
        return 0


def _ct_age(t):
    return max(0, int(time.time() - _ct(t)))


def main():
    args = sys.argv[1:]
    port = PROXY_PORT_DEFAULT
    timeout = SCHEME_TIMEOUT_DEFAULT
    for a in list(args):
        if a.startswith("--proxy-port="):
            port = int(a.split("=", 1)[1])
        elif a.startswith("--scheme-timeout="):
            timeout = int(a.split("=", 1)[1])
    mode = next((a for a in args if not a.startswith("--") and
                 a in ("run", "test", "token")), None)
    run_now = "--now" in args
    scheme_only = "--scheme-only" in args
    refresh_only = "--refresh-only" in args
    cold = "--cold" in args

    print("=" * 60)
    print("全自动抢票编排器 v3")
    if refresh_only:
        t = try_refresh_via_authcode()
        print("纯接口刷新: %s" % ("成功" if t else "失败(需走完整流程)"))
        return 0 if t else 1

    print("[预检] 尝试 auth_code 纯接口刷新(不开微信)…")
    t = try_refresh_via_authcode()
    if t:
        print("token 已就绪(纯接口刷新, 全程无微信), 年龄 %d 秒" % _ct_age(t))
    else:
        print("[预检] auth_code 不可用, 走 UI 自动化(微信界面点击)…")
        rc = subprocess.call([sys.executable, "-u", "-X", "utf8",
                              os.path.join(BASE, "ui_refresh.py")])
        if rc != 0:
            print("[!] UI 自动化抓取失败")
            return 1
        import grab_ticket as g
        t = g.load_token()
        if not t:
            print("[!] token.txt 仍为空")
            return 1

    if scheme_only:
        print("--scheme-only: 到此结束(不抢票)")
        return 0
    os.chdir(BASE)
    py = sys.executable
    cmd = [py, "grab_ticket.py", mode or "run", "--auto"]
    if run_now:
        cmd.append("--now")
    # 透传开抢时间/刷新策略参数给 grab_ticket.py
    for a in args:
        if a.startswith("--at=") or a.startswith("--refresh-min=") or a == "--force-refresh":
            cmd.append(a)
    print("-" * 60)
    print("启动抢票: %s" % " ".join(cmd[2:]))
    rc = subprocess.call(cmd)
    return rc


if __name__ == "__main__":
    sys.exit(main())
