# -*- coding: utf-8 -*-
"""
wesais 游泳馆抢票脚本 v3 (奥冠体育场馆)

v3 新增:
  - 一键 token 自动获取: python grab_ticket.py token
    自动启动 mitmdump(内置 addon), 打开小程序即自动捕获 exToken 返回的
    新 JWT 并写入 token.txt, 自动校验有效性
  - run 模式提前30分钟完成 Token 刷新，提前10分钟联网复检，之后每分钟保活
    刷新使用 auth_code 纯接口优先，
  失败则复用原 UI 自动化流程(微信发现->小程序->奥冠体育), 无需人工点击
  - token 存 token.txt, 脚本内置值仅作兜底

v2 改进(参考 scau-wyq/SwimmingTicketGrabbingAssistant):
  - 自动按"下一个0点"推算场次日期和星期对应的 product_id (无需手动传参)
  - 绕过系统代理(trust_env=False), 忘关代理也不会翻车
  - token 失效(40101/系统繁忙)立即中止, 售罄自动停止, 不浪费请求
  - 显示 token 年龄(JWT ct), 30-60分钟寿命心中有数
  - 轻量接口校验 token (/cfg/cfgCommon/getByCode)

用法 (先 conda activate ticket):
  python grab_ticket.py token                # 捕获新 token (系统代理指到 127.0.0.1:8080)
  python grab_ticket.py token --manual       # 手动粘贴 token
  python grab_ticket.py test                 # 校验 token / 查询目标场次, 不下单
  python grab_ticket.py run                  # 等下一个0点, 自动算日期+pid 抢票
  python grab_ticket.py run --now            # 立即真实下单一次(演练用!)
  python grab_ticket.py run --at=14:30       # 指定时刻开抢(测试用, 真实下单!)
  python grab_ticket.py run --at=+120        # 120秒后开抢(测试用, 真实下单!)
  python grab_ticket.py run --at=+120 --dry-run  # 测试定时/刷新, 到点不下单
  python grab_ticket.py pay                  # 处理 success_order.json 中的零元订单
  抢到零元票后会自动完成“去支付”确认；金额大于0的订单只准备支付信息并停止
  --no-auto-pay                              # 禁止零元订单自动确认
可选参数:
  --date=20260919   覆盖日期     --pid=25288   覆盖商品ID
  --qty=1           购买数量     --sku=完整SKU 手工指定SKU
  --at=HH:MM[:SS]   指定开抢时刻(默认下一个0点); --at=+N 为 N 秒后开抢
  --refresh-min=M   提前完成 Token 刷新的分钟数(默认30)
  --verify-min=M    开抢前联网复检的分钟数(默认10)
  --dry-run         等待并执行预热, 到开抢时只打印日志, 不创建订单
  --timeout=600     token 捕获最长等待秒数(token 模式)

token 自动获取前提:
  - conda 环境装有 mitmproxy (mitmdump 命令可用, 或装在任一 env 里)
  - 已安装 mitmproxy CA 证书(之前能抓包说明已装)
  - 抓包前把系统代理设为 127.0.0.1:8080, 捕获完成后记得关闭
"""

import sys
if __name__ == "__main__":
    from runtime_log import setup_logging
    setup_logging()

import os
import time
import json
import uuid
import base64
import shutil
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from decimal import Decimal, InvalidOperation

import requests

# GUI 通过管道读取 stdout；强制逐行写出，避免日志等到进程结束才出现。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                           line_buffering=True, write_through=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace",
                           line_buffering=True, write_through=True)
except Exception:
    pass

# ========================= 配置区 =========================
FALLBACK_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjdCI6MTc4OTY5NjE0NywiaWQiOiIxNzg5Njk2MTQ3MDgyOTUiLCJhaWQiOjEwMTAxLCJtaWQiOjQsInRpZCI6NSwicGFyYW1zIjoie1wiY29tcGFueV9pZFwiOjcwMSxcImFjY291bnRfdHlwZVwiOjMxLFwiYWNjb3VudF9pZFwiOjE2MzEzNDYsXCJtZW1iZXJfaWRcIjoxNjI1MDk0LFwic2FmZV9sXCI6MSxcInBlcnNvbm5lbF9pZFwiOjE2MjUwOTR9In0.3vmQVq6CarlF2VP8x8lox8UAz0shqtXKSJgNvDxR0Hk"   # 兜底: 手动抓包粘贴, 优先读 token.txt

BUSINESS_ID = "10000863"
STADIUM_ID  = "11617"
SYS_ID      = "12"
QUANTITY    = 1

# 星期 -> product_id 映射 (抓包验证, 0=周一; 与 scau-wyq 参考项目一致)
PRODUCT_ID_MAP = {0: 25294, 1: 25293, 2: 25292, 3: 25291, 4: 25289, 5: 25288, 6: 25287}
DAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

SKU_OVERRIDE = ""     # 手工指定完整SKU(含冒号和数量), 一般留空自动拼

ATTEMPTS      = 30
INTERVAL      = 0.10
SUCCESS_CODES = {200}

QUERY_URL = "https://api.wesais.com/ticket/wxTicketSale/getTicketOne"
ORDER_URL = "https://api.wesais.com/shop/order/create"
ORDER_GET_URL = "https://api.wesais.com/shop/order/get"
PAYMENT_WAY_URL = "https://api.wesais.com/cfg/cfgPaymentWay/list"
ORDER_PRICE_URL = "https://api.wesais.com/shop/order/price"
ORDER_PAY_URL = "https://api.wesais.com/shop/order/pay"
CHECK_URL = "https://api.wesais.com/cfg/cfgCommon/getByCode"
EXTOKEN_PATH = "/member/wxMember/exToken"
BASE_URL  = "https://api.wesais.com/"
TZ = timezone(timedelta(hours=8))

# ---------------- token 自动获取配置 ----------------
TOKEN_FILE     = "token.txt"
HOOK_FILE      = "token_hook.py"
PROXY_PORT     = 8080
TOKEN_FRESH_AGE = 300        # token 年龄 < 5 分钟视为"新鲜"
TOKEN_STALE_AGE = 25 * 60    # token 年龄 > 25 分钟视为"过旧", 建议刷新
CAPTURE_TIMEOUT = 600        # token 模式默认最长等待(秒)

# 内置 mitmdump addon: 捕获 exToken 响应 -> 原子写入 token.txt
HOOK_SOURCE = '''# -*- coding: utf-8 -*-
# 由 grab_ticket.py 自动生成: 捕获 miniLogin(auth_code) 与 exToken(token)
import json
import os
import time
from mitmproxy import http

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "hook_log.txt")
ALLLOG = os.path.join(BASE, "all_traffic.log")


def _atomic(name, text):
    tmp = os.path.join(BASE, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, os.path.join(BASE, name))


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S] ") + msg + chr(10))
    except Exception:
        pass


class TokenHook:
    def request(self, flow: http.HTTPFlow) -> None:
        try:
            with open(ALLLOG, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%H:%M:%S] ") + flow.request.host + " "
                        + flow.request.path.split("?")[0] + chr(10))
        except Exception:
            pass

    def response(self, flow: http.HTTPFlow) -> None:
        try:
            host = flow.request.host
            path = flow.request.path
            if host not in ("api.wesais.com", "xcx.wesais.com"):
                return
            _log("%s %s -> HTTP %s" % (host, path.split("?")[0],
                                       flow.response.status_code))
            if "/member/wxMember/miniLogin" in path:
                data = json.loads(flow.response.get_text())
                auth = (data.get("data") or {}).get("auth_code", "")
                if auth:
                    _atomic("auth_code.txt", auth)
                    _atomic("auth_code_time.txt", str(int(time.time())))
                    _log(">> got auth_code")
            elif "/member/wxMember/exToken" in path:
                data = json.loads(flow.response.get_text())
                token = (data.get("data") or {}).get("token", "")
                if isinstance(token, str) and token.startswith("eyJ"):
                    _atomic("token.txt", token)
                    _log(">> got token")
        except Exception as e:
            _log("response err: %r" % e)

    def error(self, flow: http.HTTPFlow) -> None:
        try:
            if flow.request.host in ("api.wesais.com", "xcx.wesais.com"):
                _log("CONN-ERR %s %s: %s" % (flow.request.host,
                                             flow.request.path.split("?")[0],
                                             flow.error))
        except Exception:
            pass


addons = [TokenHook()]
'''
# =========================================================

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

OFFSET = 0.0


# ==================== token 存取与自动获取 ====================

def load_token():
    """从 token.txt 读取 token, 无效返回 None"""
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            t = f.read().strip()
            return t if t.startswith("eyJ") else None
    except Exception:
        return None


def save_token(t):
    global TOKEN
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(t)
    os.replace(tmp, TOKEN_FILE)
    # GUI/后台线程可能已经导入本模块；文件更新后同步内存缓存，
    # 避免后续 new_session() 继续使用导入时的旧 token。
    TOKEN = t


TOKEN = load_token() or FALLBACK_TOKEN


def ensure_hook():
    """每次捕获前重写 addon, 保证与主脚本同步"""
    try:
        with open(HOOK_FILE, "w", encoding="utf-8") as f:
            f.write(HOOK_SOURCE)
        return True
    except Exception as e:
        print("[!] 无法写入 %s: %s" % (HOOK_FILE, e))
        return False


def find_mitmdump():
    for p in (os.path.join(sys.prefix, "Scripts", "mitmdump.exe"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".venv", "Scripts", "mitmdump.exe")):
        if os.path.isfile(p):
            return p
    exe = shutil.which("mitmdump")
    if exe:
        return exe
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, ".conda", "envs"),
        os.path.join(home, "anaconda3", "envs"),
        os.path.join(home, "miniconda3", "envs"),
        r"C:\ProgramData\anaconda3\envs",
        r"C:\ProgramData\miniconda3\envs",
    ]
    for root in roots:
        if os.path.isdir(root):
            for env in sorted(os.listdir(root)):
                p = os.path.join(root, env, "Scripts", "mitmdump.exe")
                if os.path.exists(p):
                    return p
    return None


def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    r = s.connect_ex(("127.0.0.1", port))
    s.close()
    return r == 0


def start_capture():
    mitm = find_mitmdump()
    if not mitm:
        print("[!] 未找到 mitmdump。请 conda install mitmproxy, 或改用: token --manual")
        return None, None
    if port_in_use(PROXY_PORT):
        print("[!] 端口 %d 已被占用(可能已开着 mitmproxy/其他程序)。" % PROXY_PORT)
        print("    请关闭后重试, 或改用: token --manual")
        return None, None
    cmd = [mitm, "-s", HOOK_FILE, "-p", str(PROXY_PORT), "-q"]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kwargs)
    except Exception as e:
        print("[!] mitmdump 启动失败: %s" % e)
        return None, None
    time.sleep(2)
    if proc.poll() is not None:
        print("[!] mitmdump 启动后立即退出(exit=%s), 请检查安装" % proc.returncode)
        return None, None
    return mitm, proc


def stop_capture(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
        print("捕获已停止 (mitmdump 已退出, 记得关闭系统代理!)")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def watch_new_token(timeout):
    """轮询 token.txt, 返回比当前更新且新鲜的 token, 超时返回 None"""
    baseline_ct = token_age() or 0
    print("等待新 token (最长 %d 秒)…" % timeout)
    print(">> 请现在打开 wesais 预约小程序, 随便进入一个场馆页面(会自动请求 exToken) <<")
    deadline = time.time() + timeout
    last_size = -1
    while time.time() < deadline:
        try:
            if os.path.exists(TOKEN_FILE):
                size = os.path.getsize(TOKEN_FILE)
                if size > 0 and size != last_size:
                    last_size = size
                    t = load_token()
                    ct = token_age(t) or 0
                    if t and ct < TOKEN_FRESH_AGE * 2 and (baseline_ct == 0 or ct < baseline_ct):
                        return t
        except Exception:
            pass
        print("\r  等待小程序触发 exToken… 剩余 %3d 秒" % int(deadline - time.time()),
              end="", flush=True)
        time.sleep(1)
    print()
    return None


def apply_token(t):
    """校验新 token, 有效则保存并启用"""
    global TOKEN
    old = TOKEN
    TOKEN = t
    if not validate_token(new_session(t), t):
        print("[!] 新 token 校验失败(无效), 不保存")
        TOKEN = old
        return False
    save_token(t)
    print("新 token 校验通过, 已保存到 %s (年龄 %d 秒)" % (TOKEN_FILE, token_age()))
    return True


def manual_token_input():
    print("请粘贴新的 Authorization (JWT) 后回车:")
    try:
        t = input().strip().strip('"')
    except Exception:
        return False
    if not t.startswith("eyJ"):
        print("[!] 不像 JWT(应以 eyJ 开头), 放弃")
        return False
    return apply_token(t)


def refresh_token_flow(timeout=CAPTURE_TIMEOUT, auto=False):
    """捕获新 token 的完整流程: mitmdump -> 提示操作 -> 捕获 -> 校验保存"""
    print("=" * 60)
    print("Token 自动获取")
    if not ensure_hook():
        return manual_token_input()
    mitm, proc = start_capture()
    if proc is None:
        return manual_token_input()
    print("捕获已启动: %s (监听 127.0.0.1:%d)" % (mitm, PROXY_PORT))
    print("检查清单:")
    print("  1. 系统代理已开启且指向 127.0.0.1:%d (没设过请先设置)" % PROXY_PORT)
    print("  2. 打开微信 -> wesais 预约小程序 -> 进入场馆页面")
    print("  3. 捕获成功会自动保存, 完成后请关闭系统代理")
    try:
        t = watch_new_token(timeout)
    finally:
        stop_capture(proc)
    if not t:
        print("[!] 超时未捕获到新 token")
        if auto:
            return False
        return manual_token_input()
    return apply_token(t)


def refresh_token_unattended(max_wait=150):
    """无人值守刷新: auth_code 纯接口优先, 失败则复用原 UI 流程。

    UI 流程由 ui_refresh.py 负责：微信发现页 -> 小程序 -> 逐个尝试最近使用，
    通过 api.wesais.com 流量识别“奥冠体育”，再由 mitm hook 捕获 exToken。
    不直接打开 weixin://dl/business deeplink，因为该 deeplink 在当前微信版本
    可能落到 /deeplink/noaccess 页面。
    """
    global TOKEN
    print("[auto] 开始无人值守刷新 token…")
    try:
        import auto_ticket as a
    except Exception as e:
        print("[auto] 无法导入 auto_ticket: %s" % e)
        return False
    t = a.try_refresh_via_authcode()
    if t:
        TOKEN = load_token() or t
        print("[auto] auth_code 纯接口刷新成功 (token 年龄 %d 秒)" % (token_age(TOKEN) or 0))
        return True
    print("[auto] 纯接口刷新不可用, 复用微信 UI 流程刷新…")
    ui_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_refresh.py")
    if not os.path.exists(ui_script):
        print("[auto] 找不到 ui_refresh.py")
        return False
    try:
        # ui_refresh.py 自己负责代理、mitmdump、微信窗口导航和收尾。
        # 使用当前解释器，避免 GUI/conda 环境不一致。
        rc = subprocess.call(
            [sys.executable, "-u", "-X", "utf8", ui_script],
            cwd=os.path.dirname(ui_script),
            timeout=max(1, int(max_wait) + 30),
        )
    except subprocess.TimeoutExpired:
        print("[auto] UI 刷新超时")
        return False
    except Exception as e:
        print("[auto] UI 刷新异常: %s" % e)
        return False
    if rc == 0:
        t = load_token()
        if t:
            TOKEN = t
            print("[auto] 微信 UI 刷新成功 (token 年龄 %d 秒)" % (token_age(TOKEN) or 0))
            return True
    print("[auto] 微信 UI 刷新失败 (exit=%s)" % rc)
    return False


def parse_at_target(args):
    """解析 --at=HH:MM[:SS] (今天/明天该时刻) 或 --at=+秒 (相对现在); 返回 (datetime|None, 原文)"""
    raw = None
    for a in args:
        if a.startswith("--at="):
            raw = a.split("=", 1)[1].strip()
    if not raw:
        return None, None
    now = datetime.now(TZ)
    if raw.startswith("+"):
        try:
            secs = int(raw[1:])
        except ValueError:
            raise SystemExit("[!] --at=+秒 格式错误: %r" % raw)
        if secs < 0:
            raise SystemExit("[!] --at=+秒 不能为负: %r" % raw)
        return now + timedelta(seconds=secs), raw
    parts = raw.split(":")
    try:
        if len(parts) == 2:
            hh, mm, ss = int(parts[0]), int(parts[1]), 0
        elif len(parts) == 3:
            hh, mm, ss = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            raise ValueError(raw)
    except ValueError:
        raise SystemExit("[!] --at 时间格式应为 HH:MM 或 HH:MM:SS: %r" % raw)
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise SystemExit("[!] --at 时间超出范围: %r" % raw)
    tgt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if tgt <= now:
        tgt += timedelta(days=1)
    return tgt, raw


def ensure_fresh_token(max_wait=480):
    """run 模式入口检查: 先验证有效性, 有效直接用(实测寿命远超注释值); 失效才刷新"""
    global TOKEN
    file_token = load_token()
    if file_token:
        # token.txt 可能由 GUI/mitmdump 进程刚刚原子替换。
        # 后续无参 new_session() 也必须看到这个新值。
        TOKEN = file_token
    token = file_token or TOKEN
    if token and validate_token(new_session(token), token):
        age = token_age(token)
        if age is not None:
            print("token 检查: 有效 (年龄 %d 分钟)" % (age // 60))
        else:
            print("token 检查: 有效")
        return True
    age = token_age(token)
    if age is None:
        print("[!] 没有可用 token (token.txt 不存在且内置兜底已失效)")
    else:
        print("[!] token 已失效 (年龄 %d 分钟), 需要刷新" % (age // 60))
    if "--auto" in sys.argv:
        print("[auto] 静默模式: 自动尝试刷新 token(无人值守)…")
        return refresh_token_unattended(max_wait=min(max_wait, 120))
    try:
        ans = input("现在自动获取新 token? [Y/n]: ").strip().lower()
    except Exception:
        ans = "n"
    if ans in ("n", "no"):
        return False
    return refresh_token_flow(timeout=max_wait)


def cmd_token(args):
    timeout = CAPTURE_TIMEOUT
    for a in args:
        if a.startswith("--timeout="):
            try:
                timeout = int(a.split("=", 1)[1])
            except Exception:
                pass
    if "--manual" in args:
        ok = manual_token_input()
    else:
        ok = refresh_token_flow(timeout=timeout)
    print("-" * 60)
    if ok:
        print("token 已就绪 (年龄 %d 分钟)。可运行: python grab_ticket.py test" % (token_age() // 60))
    else:
        print("token 未更新, 请重试, 或用: python grab_ticket.py token --manual")


# ==================== 基础请求工具 ====================

def rid():
    return uuid.uuid4().hex


def build_sku(pid, date_str):
    if SKU_OVERRIDE:
        return SKU_OVERRIDE
    return "121" + str(int(pid)).zfill(8) + date_str + ":" + str(QUANTITY)


def build_headers(token=None):
    """构造请求头；显式传 token 时不读取导入时的全局缓存。"""
    token = token or TOKEN
    return {
        "Authorization": token,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
                       "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
                       "MiniProgramEnv/Windows WindowsWechat/WMPF "
                       "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541d41) "
                       "XWEB/25560 miniProgram/wxdd06284882e39efb"),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "AuthRouter": "",
        "Origin": "https://xcx.wesais.com",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://xcx.wesais.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def new_session(token=None):
    s = requests.Session()
    s.trust_env = False  # 直连, 忽略系统代理(防止忘关 mitm 代理导致全挂)
    s.headers.update(build_headers(token))
    return s


def token_age(t=None):
    """从 JWT ct 字段算 token 年龄(秒), 失败返回 None"""
    t = t or TOKEN
    try:
        p = t.split(".")[1]
        p += "=" * (4 - len(p) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p))
        ct = payload.get("ct", 0)
        return time.time() - ct if ct else None
    except Exception:
        return None


def token_alive(j):
    """40101/系统繁忙 = 服务器伪装的 token 失效"""
    return isinstance(j, dict) and not token_expired(j)


def token_expired(j):
    code = j.get("code")
    msg = j.get("message") or j.get("msg") or ""
    if code == 40101 or "繁忙" in msg:
        return True
    if code in (401, 403) or "登录" in msg or "认证" in msg:
        return True
    return False


def inspect_token(s, token=None):
    """返回只读校验的 HTTP/业务结果；异常和未知业务码不再误报为有效。"""
    try:
        if token:
            s.headers.update(build_headers(token))
        r = s.post(CHECK_URL, data={
            "business_id": BUSINESS_ID, "stadium_id": "0",
            "cfg_code": "footer_set", "request_id": rid(),
        }, timeout=5)
        if not 200 <= r.status_code < 300:
            return {'ok': False, 'status': 'invalid' if r.status_code in (401, 403) else 'error',
                    'http_status': r.status_code, 'code': None,
                    'message': '校验接口 HTTP %s' % r.status_code}
        body = r.json()
        if not isinstance(body, dict):
            raise ValueError('校验接口返回的 JSON 不是对象')
        code = body.get('code')
        message = str(body.get('message') or body.get('msg') or '')
        ok = code == 200 and token_alive(body)
        return {'ok': ok, 'status': 'valid' if ok else ('invalid' if token_expired(body) else 'error'),
                'http_status': r.status_code, 'code': code,
                'message': message or ('校验通过' if ok else '校验接口未返回业务成功码 200')}
    except Exception as error:
        return {'ok': False, 'status': 'error', 'http_status': None, 'code': None,
                'message': '%s: %s' % (type(error).__name__, error)}


def validate_token(s, token=None):
    return inspect_token(s, token).get('ok', False)


def inspect_current_token():
    """每次从磁盘重读，并同步后续会话使用的 Token。"""
    global TOKEN
    token = load_token() or TOKEN
    if not token:
        return {'ok': False, 'status': 'missing', 'message': '未配置 Token', 'age': None}
    TOKEN = token
    with new_session(token) as session:
        result = inspect_token(session, token)
    result['age'] = token_age(token)
    return result


def query_body(date_str, pid):
    return {
        "business_id": BUSINESS_ID, "stadium_id": STADIUM_ID,
        "date": date_str, "ticket_product_id": str(pid), "request_id": rid(),
    }


def order_body(date_str, pid):
    return {
        "business_id": BUSINESS_ID, "stadium_id": STADIUM_ID, "sys_id": SYS_ID,
        "sku_slice": build_sku(pid, date_str), "business_type": "1201",
        "order_from": "2",
        "handle_info": json.dumps({"date_str": date_str}, separators=(",", ":")),
        "sales_id": "0", "request_id": rid(),
    }


def server_offset(s):
    samples = []
    for _ in range(3):
        t0 = time.time()
        r = s.get(BASE_URL, timeout=5)
        t1 = time.time()
        if "Date" not in r.headers:
            continue
        server = parsedate_to_datetime(r.headers["Date"]).timestamp()
        samples.append(server - (t0 + t1) / 2.0)
        time.sleep(0.1)
    if not samples:
        raise RuntimeError("拿不到服务器时间(Date头)")
    samples.sort()
    return samples[len(samples) // 2]


def server_now():
    return time.time() + OFFSET


def next_midnight(dt):
    t = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return t


def wait_until(target_epoch):
    while True:
        remain = target_epoch - server_now()
        if remain <= 0.06:
            break
        time.sleep(min(remain - 0.03, 0.5))
    deadline = time.perf_counter() + (target_epoch - server_now())
    while time.perf_counter() < deadline:
        pass


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def _post_json(s, url, data, timeout=(3, 8)):
    """提交一个业务请求并返回 (HTTP 状态, JSON 对象, 异常文本)。"""
    try:
        r = s.post(url, data=data, timeout=timeout)
        try:
            return r.status_code, r.json(), None
        except (ValueError, TypeError):
            return r.status_code, None, "响应不是 JSON"
    except requests.RequestException as e:
        return None, None, str(e)


def _decimal_value(value):
    """把接口返回的金额转换成 Decimal；无法识别时返回 None。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _order_amount(payload):
    """从订单详情/价格接口中读取应付金额。

    两个接口的响应层级不同，当前站点会在 data.pay_price 或
    data.list[].order.pay_price 中返回金额。pay_price 优先，找不到时
    再兼容 price/order_price/sale_price 字段。
    """
    if not isinstance(payload, dict):
        return None
    keys = ("pay_price", "price", "order_price", "sale_price")

    # 先读订单层字段。/shop/order/get 的 data.list.item 也有 pay_price，
    # 但它位于订单对象之前；直接递归会误把商品字段当成最终应付金额。
    objects = [payload]
    order = payload.get("order")
    if isinstance(order, dict):
        objects.append(order)
    listing = payload.get("list")
    if isinstance(listing, dict) and isinstance(listing.get("order"), dict):
        objects.append(listing["order"])
    for obj in objects:
        for key in keys:
            if key not in obj:
                continue
            amount = _decimal_value(obj.get(key))
            if amount is not None:
                return amount

    # 兼容少数响应把价格再包在 data/list/item 中的情况。
    for key in keys:
        value = find_key(payload, key)
        if value is not None:
            amount = _decimal_value(value)
            if amount is not None:
                return amount
    return None


def _payment_methods(payload):
    """提取 cfgPaymentWay/list 返回的支付方式数组。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    if "payment_way_id" in payload:
        return [payload]
    for key in ("data", "list", "items"):
        value = payload.get(key)
        methods = _payment_methods(value)
        if methods:
            return methods
    return []


def _status_number(payload):
    value = find_key(payload, "order_status")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _verify_order_paid(s, parent_order_no):
    """查询订单状态；只返回是否明确看到 order_status=1。"""
    status, body, _ = _post_json(s, ORDER_GET_URL, {
        "business_id": BUSINESS_ID,
        "stadium_id": STADIUM_ID,
        "parent_order_no": parent_order_no,
        "request_id": rid(),
    })
    return status == 200 and isinstance(body, dict) and \
        body.get("code") == 200 and _status_number(body.get("data")) == 1


def auto_pay_zero_order(s, parent_order_no):
    """自动确认零元订单，金额大于 0 时停止在支付确认之前。

    “去支付”在零元票上仍需要调用 /shop/order/pay 才会把订单从未支付
    变为已支付。这个函数严格先读取订单金额，只有明确为 0 时才提交
    支付确认请求，避免把收费订单变成自动扣款流程。
    """
    if not parent_order_no:
        print("[支付] 找不到 parent_order_no，无法自动确认")
        return False

    print("[支付] 检查订单金额和支付方式…")
    status, detail, error = _post_json(s, ORDER_GET_URL, {
        "business_id": BUSINESS_ID,
        "stadium_id": STADIUM_ID,
        "parent_order_no": parent_order_no,
        "request_id": rid(),
    })
    if error or not isinstance(detail, dict) or detail.get("code") != 200:
        message = error or (detail.get("message", "未知错误")
                            if isinstance(detail, dict) else "未知错误")
        print("[支付] 读取订单详情失败: %s" % message)
        return False

    current_status = _status_number(detail.get("data"))
    if current_status == 1:
        print("[支付] 订单已经是已支付状态")
        return True
    if current_status not in (None, 0):
        print("[支付] 订单当前状态为 %s，停止自动处理" % current_status)
        return False

    amount = _order_amount(detail.get("data"))
    if amount is not None and amount > 0:
        print("[支付] 应付金额为 %s，已停止在人工确认之前" % amount)
        return False

    status, ways_body, error = _post_json(s, PAYMENT_WAY_URL, {
        "business_id": BUSINESS_ID,
        "stadium_id": STADIUM_ID,
        "is_show": "1",
        "request_id": rid(),
    })
    if error or status != 200 or not isinstance(ways_body, dict) \
            or ways_body.get("code") != 200:
        message = error or (ways_body.get("message", "未知错误")
                            if isinstance(ways_body, dict) else "未知错误")
        print("[支付] 获取支付方式失败: %s" % message)
        return False

    methods = _payment_methods(ways_body.get("data"))
    if not methods:
        print("[支付] 没有可用支付方式")
        return False

    for method in methods:
        way = method.get("payment_way_id")
        if way in (None, ""):
            continue
        way = str(way)
        name = method.get("payment_way_name") or method.get("payment_way_code") or way
        print("[支付] 检查支付方式: %s (%s)" % (name, way))

        # 价格接口的 stadium_id=0 与小程序抓包一致。
        status, price_body, error = _post_json(s, ORDER_PRICE_URL, {
            "business_id": BUSINESS_ID,
            "stadium_id": "0",
            "parent_order_no": parent_order_no,
            "payment_way": way,
            "order_from": "2",
            "request_id": rid(),
        })
        if error or status != 200 or not isinstance(price_body, dict) \
                or price_body.get("code") != 200:
            message = error or (price_body.get("message", "支付方式不可用")
                                if isinstance(price_body, dict) else "支付方式不可用")
            print("[支付] %s 不可用: %s" % (name, message))
            continue

        amount = _order_amount(price_body.get("data"))
        if amount is None:
            print("[支付] %s 未返回明确金额，停止自动确认" % name)
            return False
        if amount > 0:
            print("[支付] 应付金额为 %s，已停止在人工确认之前" % amount)
            return False

        # 当前抓包中成功请求的字段就是这四项；extend_field 不是从响应
        # 中复制，而是按小程序固定格式生成。
        status, pay_body, error = _post_json(s, ORDER_PAY_URL, {
            "parent_order_no": parent_order_no,
            "payment_way": way,
            "business_id": BUSINESS_ID,
            "extend_field": json.dumps({
                "trade_pay_type": "umsPay.open.mini",
                "msg_type": "wx.unifiedOrder",
            }, separators=(",", ":")),
        })
        if error or status != 200 or not isinstance(pay_body, dict) \
                or pay_body.get("code") != 200:
            message = error or (pay_body.get("message", "支付确认失败")
                                if isinstance(pay_body, dict) else "支付确认失败")
            print("[支付] 零元支付失败: %s" % message)
            continue

        pay_data = pay_body.get("data") or {}
        transaction_status = str(pay_data.get("transaction_status", ""))
        transaction_msg = pay_data.get("transaction_msg") or ""
        if transaction_status == "1" or "零元" in transaction_msg:
            time.sleep(0.25)
            if _verify_order_paid(s, parent_order_no):
                print("[支付] 零元订单已自动确认，订单状态=已支付")
            else:
                # 支付接口已明确成功，状态查询可能有短暂延迟。
                print("[支付] 零元支付接口已成功，订单状态将在刷新后同步")
            return True

        print("[支付] 接口返回未完成: transaction_status=%s" % transaction_status)

    print("[支付] 没有找到可用的零元支付方式")
    return False


def pay_saved_order(args):
    """对 success_order.json 中最近一次订单执行零元确认。"""
    if not ensure_fresh_token(max_wait=300):
        print("[支付] 没有可用 token，未处理订单")
        return False
    path = "success_order.json"
    for arg in args:
        if arg.startswith("--file="):
            path = arg.split("=", 1)[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = json.load(f)
    except (OSError, ValueError) as e:
        print("[支付] 无法读取 %s: %s" % (path, e))
        return False
    parent_order_no = find_key(body, "parent_order_no")
    if not parent_order_no:
        print("[支付] %s 中没有 parent_order_no" % path)
        return False
    return auto_pay_zero_order(new_session(), parent_order_no)


def resolve_target(args, for_run, at_dt=None):
    """返回 (date_str, pid, day_name); 默认= 下一个0点的日期和星期; --at 为指定时刻"""
    now_bj = datetime.now(TZ)
    if at_dt is not None:
        tgt = at_dt
    elif for_run:
        tgt = next_midnight(now_bj)
    else:
        tgt = now_bj
    date_str = tgt.strftime("%Y%m%d")
    pid = PRODUCT_ID_MAP[tgt.weekday()]
    day = DAY_NAMES[tgt.weekday()]
    for a in args:
        if a.startswith("--date="):
            date_str = a.split("=", 1)[1]
        elif a.startswith("--pid="):
            pid = int(a.split("=", 1)[1])
            day = "指定"
    return date_str, pid, day


# ==================== 模式实现 ====================

def test_mode(args):
    s = new_session()
    date_str, pid, day = resolve_target(args, for_run=False)
    age = token_age()
    print("=" * 60)
    print("测试模式: 只查询, 不下单")
    print("目标场次: %s (%s) product_id=%s" % (date_str, day, pid))
    print("SKU(自动拼):", build_sku(pid, date_str))
    if TOKEN and TOKEN == load_token():
        print("token 来源: %s" % TOKEN_FILE)
    elif TOKEN:
        print("token 来源: 脚本内置(兜底), 建议运行 token 命令更新")
    if age is not None:
        print("token 年龄: %.0f 秒 (%.0f 分钟)%s" % (
            age, age / 60, "  [注意: 超过30分钟, 建议刷新]" if age > 1800 else ""))
    print("-" * 60)
    inspection = inspect_token(s)
    if not inspection.get('ok'):
        print('[!] 轻量校验未通过: HTTP=%s code=%s %s' % (
            inspection.get('http_status'), inspection.get('code'), inspection.get('message')))
        return
    print("轻量校验: token 有效")
    try:
        r = s.post(QUERY_URL, data=query_body(date_str, pid), timeout=10)
        j = r.json()
    except Exception as e:
        print("查询失败:", e)
        return
    print("查询 HTTP:", r.status_code)
    print(json.dumps(j, ensure_ascii=False, indent=2)[:2200])
    print("-" * 60)
    if j.get("code") == 200:
        data = j.get("data") or {}
        actual = data.get("product_id")
        if actual is not None and int(actual) != int(pid):
            print("[!] 注意: API 返回 product_id=%s 与目标 %s 不一致, 请核对映射!" % (actual, pid))
        print("code=200, 目标场次可查, 一切就绪")
    elif not token_alive(j):
        print("token 已失效, 运行: python grab_ticket.py token")
    else:
        print("业务码 %s (%s): 若是未开售/未开放类属正常, 开抢后即可下单" % (
            j.get("code"), j.get("message")))


def grab(args, run_now=False):
    global OFFSET
    s = new_session()
    auto_pay = "--no-auto-pay" not in args
    dry_run = "--dry-run" in args
    from token_schedule import prepare_scheduled_token, REFRESH_MINUTES, VERIFY_MINUTES
    refresh_min = REFRESH_MINUTES
    verify_min = VERIFY_MINUTES
    for argument in args:
        if argument.startswith(('--refresh-min=', '--verify-min=')):
            try:
                minutes = float(argument.split('=', 1)[1])
            except ValueError:
                raise SystemExit('[!] 刷新/复检参数需要有效分钟数: %r' % argument)
            if argument.startswith('--refresh-min='):
                refresh_min = minutes
            else:
                verify_min = minutes
    if not (refresh_min > verify_min > 0) or refresh_min == float('inf'):
        raise SystemExit('[!] 需要 提前刷新分钟数 > 复检分钟数 > 0（默认30、10）')
    # 立即模式(--now)不需要定时, --at 仅对等待模式生效
    at_target, at_raw = (None, None) if run_now else parse_at_target(args)
    date_str, pid, day = resolve_target(args, for_run=not run_now, at_dt=at_target)
    print("=" * 60)
    print("product_id:", pid, "(%s)" % day)
    print("日期:      ", date_str)
    print("SKU:       ", build_sku(pid, date_str))
    print("请求次数:  ", ATTEMPTS, " 间隔:", int(INTERVAL * 1000), "ms")
    age = token_age()
    if age is not None:
        print("token 年龄: %.0f 分钟%s" % (age / 60, "  [偏老, 注意]" if age > 25 * 60 else ""))
    if run_now:
        if not ensure_fresh_token(max_wait=120):
            print('[Token] 启动校验失败，取消本次请求')
            return False
        s = new_session()
    try:
        OFFSET = server_offset(s)
        print("时钟偏移:  %+.3fs (服务器 - 本机)" % OFFSET)
    except Exception as e:
        print("校时失败:", e)
        return

    if run_now:
        print("立即模式: 马上下单(真实订单!)")
    else:
        if at_target is not None:
            target = at_target.timestamp()
            print("目标(--at=%s): 本地时间 %s, 距开始 %.1f 秒" %
                  (at_raw, at_target.strftime("%Y-%m-%d %H:%M:%S"), target - time.time()))
        else:
            target = next_midnight(datetime.fromtimestamp(server_now(), TZ)).timestamp()
            print("目标: 服务器时间 00:00:00 (北京), 距开始 %.1f 秒" % (target - server_now()))
        if not prepare_scheduled_token(
                target, probe=inspect_current_token, refresh=refresh_token_unattended,
                now=server_now, sleep=time.sleep, refresh_minutes=refresh_min,
                verify_minutes=verify_min, stale_seconds=TOKEN_STALE_AGE):
            print('[Token] 准备或复检未通过，已取消抢票，未发送下单请求')
            return False
        # 准备流程可能多次更新磁盘凭据，预热和下单必须重建会话。
        s = new_session()
        try:
            r = s.post(QUERY_URL, data=query_body(date_str, pid), timeout=5)
            try:
                j = r.json()
                print("连接预热成功 (探测: code=%s %s)" % (j.get("code"), j.get("message")))
                if not token_alive(j):
                    # 距开抢仅几秒, 只做 8 秒超时的 auth_code 纯接口快速刷新,
                    # 刷新后必须重新校验，失败时停止下单
                    print("[!] 预热发现 token 已失效(或系统繁忙), 快速刷新尝试…")
                    try:
                        import auto_ticket as _a
                        _t = _a.try_refresh_via_authcode()
                    except Exception:
                        _t = None
                    if _t and inspect_current_token().get('ok'):
                        s = new_session()
                        print("[!] 快速刷新并校验成功, 使用新 token 开抢")
                    else:
                        print("[Token] 预热校验失败，取消抢票，未发送下单请求")
                        return False
            except Exception:
                print("连接预热成功 (HTTP %s)" % r.status_code)
            OFFSET = server_offset(s)
            print("二次校时:  %+.3fs" % OFFSET)
        except Exception as e:
            print("预热请求失败(不影响继续):", e)
        wait_until(target)
        print("开始抢票! 服务器时间:",
              datetime.fromtimestamp(server_now(), TZ).strftime("%H:%M:%S.%f"))
        if dry_run:
            print("[dry-run] 到点触发验证完成, 未发送下单请求")
            return True

    sold_out_seen = 0
    for i in range(1, ATTEMPTS + 1):
        stamp = datetime.fromtimestamp(server_now(), TZ).strftime("%H:%M:%S.%f")[:-3]
        try:
            r = s.post(ORDER_URL, data=order_body(date_str, pid), timeout=(3, 5))
            j = r.json()
        except Exception as e:
            print("[%d/%d] [%s] 请求异常: %s" % (i, ATTEMPTS, stamp, e))
            time.sleep(INTERVAL)
            continue
        code = j.get("code")
        msg = j.get("message") or j.get("msg") or ""
        if code in SUCCESS_CODES:
            pretty = json.dumps(j, ensure_ascii=False, indent=2)
            print("[%d/%d] [%s] >>> 成功!" % (i, ATTEMPTS, stamp))
            print(pretty)
            with open("success_order.json", "w", encoding="utf-8") as f:
                f.write(pretty)
            print("-" * 60)
            print("抢票成功! 第 %d 次, order_no: %s" % (i, find_key(j, "order_no")))
            print("订单已保存 success_order.json")
            if auto_pay:
                parent_order_no = find_key(j, "parent_order_no")
                if auto_pay_zero_order(s, parent_order_no):
                    print("[支付] 自动处理完成")
                else:
                    print("[支付] 未自动确认；如为收费订单，请在小程序中手动完成支付")
            else:
                print("[支付] 已按 --no-auto-pay 跳过自动确认")
            return True
        if token_expired(j):
            print("[%d/%d] [%s] token 已失效(40101/繁忙), 中止!" % (i, ATTEMPTS, stamp))
            print("运行 python grab_ticket.py token 刷新后再来, 动作要快!")
            return False
        if "不足" in msg:
            sold_out_seen += 1
            print("[%d/%d] [%s] 已售罄: %s" % (i, ATTEMPTS, stamp, msg))
            if sold_out_seen >= 5 and i >= 5:
                print("连续售罄, 停止重试")
                return False
        else:
            print("[%d/%d] [%s] code=%s %s" % (i, ATTEMPTS, stamp, code, msg))
        time.sleep(INTERVAL)

    print("-" * 60)
    print("%d 次尝试未成功, 开抢日期/pid 是否正确? 可加 --date= --pid= 重试" % ATTEMPTS)
    return False


if __name__ == "__main__":
    args = sys.argv[1:]
    for a in args:
        if a.startswith("--qty="):
            QUANTITY = int(a.split("=", 1)[1])
        elif a.startswith("--sku="):
            SKU_OVERRIDE = a.split("=", 1)[1]
    mode = next((a for a in args if not a.startswith("--")), "")
    if mode == "test":
        test_mode(args)
    elif mode == "run":
        sys.exit(0 if grab(args, run_now=("--now" in args)) else 1)
    elif mode == "token":
        cmd_token(args)
    elif mode == "pay":
        sys.exit(0 if pay_saved_order(args) else 1)
    else:
        print(__doc__)
