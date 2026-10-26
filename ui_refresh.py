# -*- coding: utf-8 -*-
"""
ui_refresh.py v8
通过 UI 自动化打开 wesais 小程序以捕获 token。
不假设小程序在面板中的位置: 逐个尝试图标, 用网络证据(api.wesais.com 流量)判断
是否开对了, 开错则关闭窗口换下一个。通过窗口边界和像素布局识别发现/小程序入口。
"""
import os
import sys
if __name__ == "__main__":
    from runtime_log import setup_logging
    setup_logging()

import time
import subprocess
import importlib.util

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace',
                           line_buffering=True, write_through=True)
    sys.stderr.reconfigure(encoding='utf-8', errors='replace',
                           line_buffering=True, write_through=True)
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from ui_layout import (
    LayoutDetectionError, detect_content_layout, detect_discover_point,
    detect_miniprogram_point, detect_recent_grid_points,
)
from ui_window import (
    configure_dpi_awareness, focus_window, snap, layout_metrics,
    image_point_to_screen, click_image_at, click_screen_point,
)


def find_main_window():
    from pywinauto import Desktop
    candidates = Desktop(backend='uia').windows(title_re=r'^(微信|WeChat|Weixin)$')
    candidates = [win for win in candidates
                  if win.class_name() in ('Qt51514QWindowIcon', 'WeChatMainWndForPC')]
    if len(candidates) == 1:
        return candidates[0]
    print('[ui] 微信主窗口候选数: %d' % len(candidates))
    return None


def named_control_point(win, labels):
    import re
    expression = r'^(?:%s)$' % '|'.join(re.escape(label) for label in labels)
    try:
        frame = win.rectangle()
        controls = win.descendants(title_re=expression)
        for control in controls:
            rect = control.rectangle()
            if control.element_info.control_type in ('Button', 'TabItem', 'ListItem', 'Hyperlink') and \
                    control.is_visible() and control.is_enabled() and \
                    rect.width() > 0 and rect.height() > 0 and \
                    frame.left <= rect.left < rect.right <= frame.right and \
                    frame.top <= rect.top < rect.bottom <= frame.bottom:
                return round((rect.left + rect.right) / 2), round((rect.top + rect.bottom) / 2)
    except Exception:
        pass
    return None


def click_navigation(win, labels, detector):
    focus_window(win)
    point = named_control_point(win, labels)
    if point is not None:
        print('[ui] 按控件名称定位 %s: screen=%s' % (labels[0], point))
        click_screen_point(point)
        return
    for attempt in range(2):
        image = snap(win)
        metrics = layout_metrics(win, image)
        x, y, details = detector(image)
        print('[ui] 识别%s: image=(%d,%d), 窗口=%s, 布局=%s' %
              (labels[0], x, y, metrics[0], details))
        try:
            click_image_at(win, (x, y), metrics)
            return
        except LayoutDetectionError:
            if attempt:
                raise


def navigate_to_panel(win):
    for attempt in range(3):
        try:
            # 每轮重新定位发现；页面验证失败时，不继续在收藏等错误页面盲点。
            click_navigation(win, ('发现', 'Discover'), detect_discover_point)
            time.sleep(1)
            click_navigation(win, ('小程序', 'Mini Programs', 'Mini programs'),
                             detect_miniprogram_point)
            for _ in range(10):
                time.sleep(1)
                if detect_recent_grid_points(snap(win)):
                    print('[ui] 小程序面板已渲染 (第%d轮)' % (attempt + 1))
                    return True
            print('[ui] 小程序面板尚未渲染 (%d/3)' % (attempt + 1))
        except LayoutDetectionError as error:
            print('[ui] 导航识别失败 (%d/3): %s' % (attempt + 1, error))
        time.sleep(0.5)
    print('[ui] 无法确认小程序面板，停止刷新；请保持微信主窗口可见后重试')
    return False


def traffic_size(all_log):
    try:
        return os.path.getsize(all_log)
    except OSError:
        return 0


def traffic_has_wesais(all_log, offset):
    try:
        with open(all_log, 'rb') as f:
            f.seek(offset)
            return b'api.wesais.com' in f.read()
    except Exception:
        return False


def close_mini_windows():
    """关闭已打开的小程序独立窗口(标题非空的 Chrome_WidgetWin_0)"""
    from pywinauto import Desktop
    closed = 0
    try:
        desk = Desktop(backend='uia')
        for w in desk.windows():
            try:
                if w.class_name() == 'Chrome_WidgetWin_0':
                    t = (w.window_text() or '').strip()
                    if t:
                        w.close()
                        closed += 1
            except Exception:
                pass
    except Exception:
        pass
    if closed:
        time.sleep(1)
    return closed


def stop_process_image(image_name, timeout=3):
    """尽力结束指定进程；taskkill 卡住时不中断 token 刷新流程。"""
    try:
        result = subprocess.run(
            ['taskkill', '/f', '/im', image_name],
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print('[ui] 清理 %s 超时, 继续执行 UI 导航' % image_name)
    except Exception as e:
        print('[ui] 清理 %s 失败: %s, 继续执行 UI 导航' % (image_name, e))
    return False


def open_until_token(win, all_log, before_token, max_wait_token=15):
    import grab_ticket as g
    focus_window(win)
    image = snap(win)
    metrics = layout_metrics(win, image)
    _rect, width, height, _sx, _sy = metrics
    positions = detect_recent_grid_points(image)
    if not positions:
        print('[ui] 小程序面板中未识别到可点击图标')
        return None
    print('[ui] 使用当前窗口 %dx%d 的真实图标布局' % (width, height))
    print('[ui] 最近使用网格: %s' % positions)
    total = len(positions)
    for idx, pos in enumerate(positions):
        off = traffic_size(all_log)
        print('[ui] 尝试面板图标 %d/%d …' % (idx + 1, total))
        # 面板可能在等待期间发生重排；每次点击前重新取得截图和映射。
        image = snap(win)
        metrics = layout_metrics(win, image)
        positions_now = detect_recent_grid_points(image)
        if idx >= len(positions_now):
            print('[ui] 面板布局已变化，重新导航后再试')
            return None
        pos = positions_now[idx]
        print('[ui]   点击 image=(%d,%d)' % pos)
        click_image_at(win, pos, metrics)
        hit = False
        for _ in range(8):
            time.sleep(1)
            if traffic_has_wesais(all_log, off):
                hit = True
                break
        if not hit:
            print('[ui]   非 wesais 小程序, 关闭换下一个')
            close_mini_windows()
            continue
        print('[ui]   检测到 api.wesais.com 流量, 开对了! 等 token…')
        deadline = time.time() + max_wait_token
        while time.time() < deadline:
            t = g.load_token()
            if t and t != before_token:
                return t
            time.sleep(1)
        # 有 wesais 流量但 token 未更新(可能是首次账号异常), 继续算成功但无新token
        return g.load_token() or before_token
    return None


def main():
    configure_dpi_awareness()
    import grab_ticket as g
    import auto_ticket as a
    mitm = a._find_mitmdump()
    missing = []
    if not mitm:
        missing.append('mitmproxy (mitmdump)')
    for module, package in (('pywinauto', 'pywinauto'), ('PIL', 'Pillow')):
        if importlib.util.find_spec(module) is None:
            missing.append(package)
    if missing:
        print('[ui] 缺少刷新依赖: %s' % ', '.join(missing))
        print('[ui] 当前 Python: %s' % sys.executable)
        python = os.path.join(os.path.dirname(sys.executable), 'python.exe')
        print('[ui] 请在此环境安装依赖: "%s" -m pip install -r "%s"' %
              (python, os.path.join(BASE, 'requirements.txt')))
        return 1
    print('[ui] 使用抓包工具: %s' % mitm)
    if not g.ensure_hook():
        print('[ui] hook 写入失败')
        return 1
    if a.port_in_use(8080):
        print('[ui] 端口被占, 清理残留 mitmdump…')
        stop_process_image('mitmdump.exe')
        time.sleep(2)
        if a.port_in_use(8080):
            print('[ui] 端口仍被占用(非 mitmdump), 放弃')
            return 1
    win = find_main_window()
    if win is None:
        try:
            os.startfile('weixin://')
            time.sleep(6)
            win = find_main_window()
        except Exception:
            pass
    if win is None:
        print('[ui] 找不到微信主窗口')
        return 1

    hook_log = os.path.join(BASE, 'hook_log.txt')
    all_log = os.path.join(BASE, 'all_traffic.log')
    for p in (hook_log, all_log):
        try:
            os.remove(p)
        except Exception:
            pass
    old_enabled = a.proxy_enabled()
    old_server = a.proxy_server()
    print('[ui/1] 开启代理+mitm+冷启动小程序运行时…')
    a.proxy_on(8080)
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = 0x08000000
    proc = subprocess.Popen([mitm, '-s', g.HOOK_FILE, '-p', '8080', '-q'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=BASE, **kwargs)
    before_token = g.load_token()
    token = None
    try:
        time.sleep(2)
        if proc.poll() is not None:
            print('[ui] mitmdump 启动失败')
            return 1
        # 杀掉旧小程序运行时(面板也是它渲染的, 杀后需重新导航)
        stop_process_image('WeChatAppEx.exe')
        time.sleep(2)
        # 清掉上一次 deeplink/错误页留下的独立小程序窗口，避免它遮挡
        # 微信主窗口或让后续点击继续停留在“当前页面无法访问”。
        close_mini_windows()
        print('[ui/2] 代理环境导航到小程序面板(重建渲染)…')
        if not navigate_to_panel(win):
            return 1
        print('[ui/3] 逐个尝试面板图标(以流量识别 wesais)…')
        token = open_until_token(win, all_log, before_token)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if old_enabled and old_server and old_server != '127.0.0.1:8080':
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               a.INTERNET_SETTINGS, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.SetValueEx(k, 'ProxyEnable', 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, 'ProxyServer', 0, winreg.REG_SZ, old_server)
            finally:
                winreg.CloseKey(k)
            a._notify_proxy_change()
        else:
            a.proxy_off()
        close_mini_windows()
    if token and token != before_token:
        g.save_token(token)
        print('[ui] 成功! 新 token 年龄 %d 秒' % int(g.token_age()))
        for line in a.tail_log(hook_log, 6):
            print('  | ' + line)
        return 0
    print('[ui] 未捕获到新 token')
    for line in a.tail_log(all_log, 10):
        print('  | ' + line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
