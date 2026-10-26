# -*- coding: utf-8 -*-
"""开抢前的 Token 准备与联网复检；时间及业务动作可注入以离线验证。"""
import time


REFRESH_MINUTES = 30
VERIFY_MINUTES = 10
PREPARE_SECONDS = 180
CHECK_INTERVAL = 60


def prepare_scheduled_token(target, *, probe, refresh, now, sleep=time.sleep,
                            log=print, refresh_minutes=REFRESH_MINUTES,
                            verify_minutes=VERIFY_MINUTES, stale_seconds=25 * 60):
    """先完成强制刷新，再复检并保活到预热阶段；任何未确认有效的状态均返回失败。"""
    refresh_deadline = target - refresh_minutes * 60
    verify_deadline = target - verify_minutes * 60

    def wait_to(when):
        while now() < when:
            sleep(max(0, min(1.0, when - now())))

    def check(label):
        result = probe()
        log('[Token] %s: %s (HTTP=%s, code=%s, %s)' % (
            label, '有效' if result.get('ok') else '未通过',
            result.get('http_status', '-'), result.get('code', '-'),
            result.get('message', '')))
        return result

    def renew(label, deadline):
        available = deadline - now()
        # 接口刷新最多约 8 秒；UI 子进程有 30 秒额外收尾预算，复检最多 5 秒。
        if available < 45:
            log('[Token] %s: 刷新预算不足，取消本次抢票' % label)
            return False
        log('[Token] %s: 启动自动刷新，完成后必须联网校验' % label)
        if not refresh(max_wait=min(120, available - 45)):
            log('[Token] %s: 刷新失败，取消本次抢票' % label)
            return False
        result = check(label + '刷新后校验')
        if not result.get('ok') or now() > deadline:
            log('[Token] %s: 未在准备期限内确认 Token 有效，取消本次抢票' % label)
            return False
        return True

    log('[Token] 准备策略: 提前 %g 分钟完成刷新，提前 %g 分钟联网复检' %
        (refresh_minutes, verify_minutes))
    first_start = refresh_deadline - PREPARE_SECONDS
    if first_start - now() > PREPARE_SECONDS:
        if not check('启动校验').get('ok'):
            if not renew('启动补刷', min(first_start, now() + PREPARE_SECONDS)):
                return False
    wait_to(first_start)

    if now() >= refresh_deadline:
        log('[Token] 已错过提前 %g 分钟的刷新期限，立即补做刷新和校验' % refresh_minutes)
        first_limit = target - 45
    else:
        first_limit = refresh_deadline
    if not renew('首轮', first_limit):
        return False

    wait_to(verify_deadline - PREPARE_SECONDS)
    result = check('复检准备')
    age = result.get('age')
    # 首轮 Token 到开抢时可能已过旧；提早补刷，给十分钟检查点留出操作时间。
    projected_age = None if age is None else age + max(0, target - now())
    if not result.get('ok') or projected_age is None or projected_age > stale_seconds:
        limit = verify_deadline if now() < verify_deadline else target - 45
        if not renew('复检准备', limit):
            return False

    if now() > verify_deadline:
        log('[Token] 已进入最后 %g 分钟，立即执行联网复检' % verify_minutes)
    wait_to(verify_deadline)
    if not check('开抢前 %g 分钟复检' % verify_minutes).get('ok'):
        if not renew('复检补刷', target - 45):
            return False

    # 服务端可随时令凭据失效，因此十分钟检查后仍需持续确认。
    while target - now() > 5.5:
        wait_to(min(now() + CHECK_INTERVAL, target - 5.5))
        if target - now() <= 5.5:
            break
        if not check('开抢前保活校验').get('ok'):
            if not renew('保活补刷', target - 45):
                return False
    return True
