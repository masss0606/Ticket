"""基于实际微信布局回归截图，验证 DPI、窗口尺寸和多屏坐标；不注入真实输入。"""
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from PIL import Image, ImageDraw, ImageOps

import ui_layout as layout
import ui_refresh
import ui_window as window


FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def load(name):
    with Image.open(FIXTURES / name) as image:
        return image.convert('RGB')


class LayoutTests(unittest.TestCase):
    def test_reported_favorites_screenshot_finds_discover_and_rejects_wrong_menu(self):
        image = load('wechat_favorites.png')
        x, y, _details = layout.detect_discover_point(image)
        self.assertLessEqual(abs(x - 30), 3)
        self.assertLessEqual(abs(y - 255), 4)
        with self.assertRaises(layout.LayoutDetectionError):
            layout.detect_miniprogram_point(image)
        self.assertEqual(layout.detect_recent_grid_points(image), [])

    def test_navigation_and_grid_at_multiple_dpi_scales(self):
        source = load('wechat_miniprograms.png')
        for scale in (0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3):
            with self.subTest(scale=scale):
                image = source.resize((round(source.width * scale), round(source.height * scale)),
                                      Image.Resampling.LANCZOS)
                x, y, _details = layout.detect_discover_point(image)
                self.assertLessEqual(abs(x - 30 * scale), 4 * scale)
                self.assertLessEqual(abs(y - 255 * scale), 5 * scale)
                _x, y, _details = layout.detect_miniprogram_point(image)
                self.assertLessEqual(abs(y - 296 * scale), 5 * scale)
                points = layout.detect_recent_grid_points(image)
                self.assertEqual(len(points), 8, points)
                self.assertLessEqual(abs(points[0][0] - 380 * scale), 5 * scale)
                self.assertLessEqual(abs(points[0][1] - 180 * scale), 5 * scale)

    def test_taller_and_wider_windows_do_not_stretch_navigation_positions(self):
        source = load('wechat_miniprograms.png')
        for width, height in ((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)):
            with self.subTest(size=(width, height)):
                image = Image.new('RGB', (width, height), (250, 250, 250))
                draw = ImageDraw.Draw(image)
                draw.rectangle((0, 0, 60, height), fill=(218, 220, 225))
                draw.rectangle((61, 0, 300, height), fill=(237, 237, 239))
                image.paste(source, (0, 0))
                x, y, _details = layout.detect_discover_point(image)
                self.assertLessEqual(abs(x - 30), 3)
                self.assertLessEqual(abs(y - 255), 4)
                _x, mini_y, _details = layout.detect_miniprogram_point(image)
                self.assertLessEqual(abs(mini_y - 296), 4)

    def test_six_column_grid_uses_actual_icon_locations(self):
        image = Image.new('RGB', (1100, 650), (250, 250, 250))
        image.paste(load('wechat_miniprograms.png').crop((0, 0, 302, 586)), (0, 0))
        draw = ImageDraw.Draw(image)
        expected = [(x, y) for y in (180, 353) for x in (380, 500, 620, 740, 860, 980)]
        for x, y in expected:
            draw.ellipse((x - 23, y - 23, x + 23, y + 23), fill=(30, 120, 220))
        points = layout.detect_recent_grid_points(image)
        self.assertEqual(len(points), 12, points)
        for actual, target in zip(points, expected):
            self.assertLessEqual(abs(actual[0] - target[0]), 2)
            self.assertLessEqual(abs(actual[1] - target[1]), 2)

    def test_blank_panel_does_not_create_click_targets(self):
        image = load('wechat_miniprograms.png')
        ImageDraw.Draw(image).rectangle((302, 33, image.width - 1, image.height - 1),
                                       fill=(250, 250, 250))
        self.assertEqual(layout.detect_recent_grid_points(image), [])

    def test_dark_background_keeps_navigation_geometry(self):
        image = ImageOps.invert(load('wechat_miniprograms.png'))
        _x, y, _details = layout.detect_discover_point(image)
        self.assertLessEqual(abs(y - 255), 4)
        _x, y, _details = layout.detect_miniprogram_point(image)
        self.assertLessEqual(abs(y - 296), 4)

    def test_empty_or_unrecognizable_capture_is_rejected(self):
        for size in ((0, 0), (1, 1), (800, 600)):
            with self.subTest(size=size), self.assertRaises(layout.LayoutDetectionError):
                layout.detect_discover_point(Image.new('RGB', size, 'white'))


class CoordinateTests(unittest.TestCase):
    def test_virtual_desktop_origins_and_capture_scaling(self):
        for origin in ((0, 0), (-1920, 0), (0, -1080), (-3840, -2160), (3840, 300)):
            for scale in (1, 1.25, 1.5, 2):
                with self.subTest(origin=origin, scale=scale):
                    left, top = origin
                    rect = window.ScreenRect(left, top, left + round(800 * scale),
                                             top + round(600 * scale))
                    actual = window.image_point_to_screen(None, (30, 254), (rect, 800, 600, 1, 1))
                    self.assertEqual(actual, (left + round(30 * scale), top + round(254 * scale)))

    def test_window_moved_after_capture_updates_origin_before_click(self):
        old = window.ScreenRect(0, 0, 800, 600)
        current = window.ScreenRect(-1920, -1080, -1120, -480)
        with patch.object(window, 'focus_window'), \
                patch.object(window, 'window_rect', return_value=current), \
                patch.object(window, 'click_screen_point') as click:
            window.click_image_at(Mock(), (30, 254), (old, 800, 600, 1, 1))
            click.assert_called_once_with((-1890, -826))

    def test_resize_or_dpi_change_requires_new_capture(self):
        old = window.ScreenRect(0, 0, 800, 600)
        current = window.ScreenRect(0, 0, 1200, 900)
        with patch.object(window, 'focus_window'), \
                patch.object(window, 'window_rect', return_value=current), \
                patch.object(window, 'click_screen_point') as click:
            with self.assertRaises(window.LayoutDetectionError):
                window.click_image_at(Mock(), (30, 254), (old, 800, 600, 1, 1))
            click.assert_not_called()

    def test_click_preserves_negative_physical_coordinates(self):
        import win32api
        import win32con
        with patch.object(win32api, 'SetCursorPos') as move, \
                patch.object(win32api, 'mouse_event') as event:
            window.click_screen_point((-1900, -900))
            move.assert_called_once_with((-1900, -900))
            self.assertEqual([call.args[0] for call in event.call_args_list],
                             [win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP])

    def test_capture_uses_all_screens_and_remembers_its_bounds(self):
        rect = window.ScreenRect(-1000, -600, -200, 0)
        with patch.object(window, 'window_rect', return_value=rect), \
                patch('PIL.ImageGrab.grab', return_value=Image.new('RGB', (800, 600))) as grab:
            image = window.snap(Mock())
        grab.assert_called_once_with(bbox=(-1000, -600, -200, 0), all_screens=True)
        metrics = window.layout_metrics(None, image)
        self.assertEqual(metrics[0], rect)
        self.assertEqual(window.image_point_to_screen(None, (30, 254), metrics), (-970, -346))

    def test_none_capture_has_actionable_error(self):
        with patch.object(window, 'window_rect', return_value=window.ScreenRect(0, 0, 800, 600)), \
                patch('PIL.ImageGrab.grab', return_value=None):
            with self.assertRaisesRegex(window.LayoutDetectionError, '截图为空'):
                window.snap(Mock())

    def test_out_of_bounds_target_is_rejected(self):
        metrics = (window.ScreenRect(0, 0, 800, 600), 800, 600, 1, 1)
        with self.assertRaises(window.LayoutDetectionError):
            window.image_point_to_screen(None, (800, 254), metrics)

    def test_named_control_is_used_before_pixel_detection(self):
        detector = Mock()
        with patch.object(ui_refresh, 'focus_window'), \
                patch.object(ui_refresh, 'named_control_point', return_value=(-1900, 255)), \
                patch.object(ui_refresh, 'click_screen_point') as click:
            ui_refresh.click_navigation(Mock(), ('发现',), detector)
            click.assert_called_once_with((-1900, 255))
            detector.assert_not_called()


if __name__ == '__main__':
    unittest.main()
