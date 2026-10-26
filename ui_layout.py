# -*- coding: utf-8 -*-
"""微信布局识别：尺寸取自当前图像，不使用屏幕分辨率或窗口高宽比。"""
from statistics import median


class LayoutDetectionError(RuntimeError):
    pass


def _distance(a, b):
    return sum((a[i] - b[i]) ** 2 for i in range(3))


def _runs(values, gap=2, minimum=2):
    if not values:
        return []
    result = []
    start = previous = values[0]
    for value in values[1:]:
        if value - previous > gap:
            if previous - start + 1 >= minimum:
                result.append((start, previous))
            start = value
        previous = value
    if previous - start + 1 >= minimum:
        result.append((start, previous))
    return result


def detect_content_layout(image):
    """用跨多行持续存在的边界定位侧栏，避免把头像/菜单文字当成边界。"""
    image = image.convert('RGB')
    width, height = image.size
    if width < 160 or height < 160:
        raise LayoutDetectionError('微信窗口截图过小或窗口已最小化')
    pixels = image.load()
    ys = [round(height * (0.16 + i * 0.035)) for i in range(20)]
    edges = []
    for x in range(24, width - 8):
        votes = sum(_distance(pixels[x - 2, y], pixels[x + 2, y]) > 12 ** 2
                    for y in ys)
        if votes >= len(ys) * 0.65:
            edges.append(x)
    borders = [round((a + b) / 2) for a, b in _runs(edges)]
    rails = [x for x in borders if 32 <= x <= width * 0.25]
    if not rails:
        raise LayoutDetectionError('无法识别微信左侧导航栏，请保持微信主窗口可见')
    rail = rails[0]
    xs = [round(rail + (width - rail - 16) * (i + 1) / 13) for i in range(12)]
    rows = []
    for y in range(max(10, round(rail * 0.30)), min(height - 2, round(rail * 1.1))):
        votes = sum(_distance(pixels[x, y - 2], pixels[x, y + 2]) > 12 ** 2
                    for x in xs)
        if votes >= len(xs) * 0.55:
            rows.append(y)
    tops = _runs(rows)
    top = round(sum(tops[0]) / 2) if tops else round(rail * 0.53)
    # 菜单边界另取标题附近的背景区，避开收藏页滚动条和右侧卡片边缘。
    header_ys = [min(height - 1, round(top + rail * (0.10 + i * 0.035)))
                 for i in range(20)]
    edges = []
    for x in range(round(rail * 2.5), min(width - 3, round(width * 0.80))):
        votes = sum(_distance(pixels[x - 2, y], pixels[x + 2, y]) > 12 ** 2
                    for y in header_ys)
        if votes >= len(header_ys) * 0.65:
            edges.append(x)
    menus = [round((a + b) / 2) for a, b in _runs(edges)]
    menu_right = menus[0] if menus else min(width - 1, round(rail * 5))
    return top, rail, menu_right


def _icon_rows(image, left, right, top, bottom, background_x, scale):
    """每行各取背景色，兼容渐变侧栏、选中背景和通知红点。"""
    image = image.convert('RGB')
    pixels = image.load()
    width, height = image.size
    left, right = max(0, round(left)), min(width, round(right))
    hits = []
    for y in range(max(0, round(top)), min(height, round(bottom))):
        background = pixels[round(background_x), y]
        count = sum(_distance(pixels[x, y], background) > 28 ** 2
                    for x in range(left, right))
        if count >= max(2, round((right - left) * 0.07)):
            hits.append(y)
    return _runs(hits, gap=max(2, round(3 * scale)), minimum=max(3, round(5 * scale)))


def detect_discover_point(image):
    top, rail, _menu = detect_content_layout(image)
    scale = rail / 60.0
    runs = _icon_rows(image, rail * 0.23, rail * 0.80,
                      top + rail * 0.90,
                      min(image.height - rail * 1.3, top + rail * 6),
                      rail * 0.08, scale)
    # 微信主导航依次为聊天、通讯录、收藏、发现；排除顶部头像和底部工具。
    if len(runs) < 4:
        raise LayoutDetectionError('未识别到完整的微信主导航，停止点击以避免误操作')
    a, b = runs[3]
    return round(rail / 2), round((a + b) / 2), (top, rail, runs)


def detect_miniprogram_point(image):
    top, rail, menu = detect_content_layout(image)
    scale = rail / 60.0
    runs = _icon_rows(image, rail * 1.24, rail * 1.66,
                      top + rail * 0.80, image.height - rail * 0.15,
                      rail * 1.10, scale)
    centers = [round((a + b) / 2) for a, b in runs]
    # 发现页有五行等距入口。收藏页等其它页面不满足这个条件，不能继续猜坐标。
    if len(centers) != 5:
        raise LayoutDetectionError('尚未进入发现页（识别到 %d 行菜单，预期 5 行）' % len(centers))
    gaps = [b - a for a, b in zip(centers, centers[1:])]
    spacing = median(gaps)
    if not rail * 0.55 <= spacing <= rail * 1.15 or \
            max(abs(gap - spacing) for gap in gaps) > spacing * 0.20:
        raise LayoutDetectionError('发现页菜单间距不匹配，停止点击以避免误操作')
    return round(rail * 1.65), centers[4], (top, rail, menu, centers)


def detect_recent_grid_points(image):
    """只返回已渲染的图标，不在空白页/收藏页生成虚构网格。"""
    try:
        detect_miniprogram_point(image)
    except LayoutDetectionError:
        return []
    image = image.convert('RGB')
    top, rail, menu = detect_content_layout(image)
    pixels = image.load()
    scale = rail / 60.0

    def colorful(x, y):
        rgb = pixels[x, y]
        return max(rgb) - min(rgb) >= 35 and max(rgb) >= 70

    first_top = round(top + rail * 1.4)
    first_bottom = min(image.height, round(top + rail * 3.4))
    hits = []
    for x in range(round(menu + rail * 0.2), max(round(menu + rail * 0.2), image.width - round(rail * 0.2))):
        count = sum(colorful(x, y) for y in range(first_top, first_bottom))
        if count >= max(5, round(6 * scale)):
            hits.append(x)
    columns = [round((a + b) / 2)
               for a, b in _runs(hits, gap=max(3, round(12 * scale)), minimum=round(16 * scale))
               if b - a < rail * 1.5]
    if not columns:
        return []

    rows = []
    for begin, end in ((1.4, 3.4), (4.2, 6.5)):
        y_hits = []
        for y in range(round(top + rail * begin), min(image.height, round(top + rail * end))):
            count = sum(colorful(x, y)
                        for cx in columns
                        for x in range(max(menu, cx - round(18 * scale)),
                                       min(image.width, cx + round(18 * scale))))
            if count >= max(8, round(len(columns) * 3 * scale)):
                y_hits.append(y)
        candidates = _runs(y_hits, gap=max(2, round(3 * scale)), minimum=round(14 * scale))
        if candidates:
            a, b = max(candidates, key=lambda run: run[1] - run[0])
            rows.append(round((a + b) / 2))
    return [(x, y) for y in rows for x in columns
            if any(colorful(px, y) for px in range(max(0, x - round(18 * scale)),
                                                  min(image.width, x + round(18 * scale))))]
