# -*- coding: utf-8 -*-
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
