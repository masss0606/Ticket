# -*- coding: utf-8 -*-
"""统一使用物理屏幕坐标，支持不同 DPI 和虚拟桌面的负坐标。"""
import ctypes
from dataclasses import dataclass
import os

from ui_layout import LayoutDetectionError


@dataclass(frozen=True)
class ScreenRect:
    left: int
    top: int
    right: int
    bottom: int

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


def configure_dpi_awareness():
    """在导入 pywinauto 前启用 Per-Monitor V2；旧版 Windows 回退到 V1。"""
    if os.name != 'nt':
        return
    user32 = ctypes.windll.user32
    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        set_context(ctypes.c_void_p(-4))
        set_thread = user32.SetThreadDpiAwarenessContext
        set_thread.argtypes = [ctypes.c_void_p]
        set_thread.restype = ctypes.c_void_p
        if set_thread(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


def window_rect(win):
    rect = win.rectangle()
    result = ScreenRect(rect.left, rect.top, rect.right, rect.bottom)
    # UIA 的外框可能包含 8px 不可见调整边框，截图和坐标统一取可见窗口边界。
    if os.name == 'nt':
        from ctypes import wintypes
        visible = wintypes.RECT()
        try:
            status = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(win.handle), 9, ctypes.byref(visible), ctypes.sizeof(visible))
            if status == 0 and visible.right > visible.left and visible.bottom > visible.top:
                result = ScreenRect(visible.left, visible.top, visible.right, visible.bottom)
        except (AttributeError, OSError, TypeError):
            pass
    if result.width() <= 0 or result.height() <= 0:
        raise LayoutDetectionError('微信窗口不可见或已最小化，无法截图')
    return result


def focus_window(win):
    if win.is_minimized():
        win.restore()
    win.set_focus()


def snap(win):
    from PIL import ImageGrab
    rect = window_rect(win)
    try:
        image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                               all_screens=True)
    except Exception as error:
        raise LayoutDetectionError('微信截图失败: %s' % error) from error
    if image is None or image.width <= 0 or image.height <= 0:
        raise LayoutDetectionError('微信截图为空，请恢复微信主窗口后重试')
    image.info['screen_rect'] = (rect.left, rect.top, rect.right, rect.bottom)
    return image.convert('RGB')


def layout_metrics(win, image=None):
    if image is None:
        image = snap(win)
    rect = (ScreenRect(*image.info['screen_rect'])
            if 'screen_rect' in image.info else window_rect(win))
    width, height = image.size
    if width <= 0 or height <= 0:
        raise LayoutDetectionError('微信截图为空')
    return rect, width, height, rect.width() / width, rect.height() / height


def image_point_to_screen(win, image_point, metrics=None):
    if metrics is None:
        metrics = layout_metrics(win)
    rect, width, height, _sx, _sy = metrics
    x, y = image_point
    if not (0 <= x < width and 0 <= y < height):
        raise LayoutDetectionError('识别坐标超出微信截图范围: %s' % (image_point,))
    return (rect.left + round(x * rect.width() / width),
            rect.top + round(y * rect.height() / height))


def click_screen_point(point):
    import win32api
    import win32con
    # 不使用 pywinauto.mouse.click 的主屏绝对坐标归一化，保留副屏负坐标。
    win32api.SetCursorPos(point)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def click_image_at(win, image_point, metrics=None):
    focus_window(win)
    if metrics is None:
        metrics = layout_metrics(win)
    old_rect, width, height, sx, sy = metrics
    current = window_rect(win)
    if (current.width(), current.height()) != (old_rect.width(), old_rect.height()):
        raise LayoutDetectionError('微信窗口尺寸或 DPI 已变化，需要重新截图识别')
    # 截图后窗口被移动时，保留图像坐标，只更新窗口在虚拟桌面的原点。
    point = image_point_to_screen(win, image_point, (current, width, height, sx, sy))
    click_screen_point(point)
