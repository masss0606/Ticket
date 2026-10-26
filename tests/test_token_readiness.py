"""模拟时间与接口响应，验证提前准备流程；不访问真实业务接口。"""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import unittest
from unittest.mock import Mock, patch

import grab_ticket as g
from token_schedule import prepare_scheduled_token


class TokenInspectionTests(unittest.TestCase):
    def inspect(self, body, http=200):
        session = Mock()
        session.post.return_value = Mock(status_code=http, json=Mock(return_value=body))
        result = g.inspect_token(session, 'test-token')
        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.kwargs['timeout'], 5)
        return result

    def test_only_successful_http_and_business_response_is_valid(self):
        self.assertTrue(self.inspect({'code': 200, 'message': '成功'})['ok'])
        for http, body in ((500, {'code': 200}), (401, {'code': 200}),
                           (200, {'code': 40101}), (200, {'code': 403}),
                           (200, {'code': 999}), (200, {}), (200, []),
                           (200, {'code': 200, 'message': '系统繁忙'})):
            with self.subTest(http=http, body=body):
                self.assertFalse(self.inspect(body, http)['ok'])

    def test_network_and_json_errors_remain_visible(self):
        session = Mock()
        session.post.side_effect = TimeoutError('network timeout')
        result = g.inspect_token(session)
        self.assertEqual(result['status'], 'error')
        self.assertIn('TimeoutError', result['message'])
        session.post.side_effect = None
        session.post.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError('bad JSON')))
        self.assertIn('bad JSON', g.inspect_token(session)['message'])

    def test_current_probe_reads_new_disk_token_and_updates_future_sessions(self):
        session = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=session)
        context.__exit__ = Mock(return_value=False)
        with patch.object(g, 'TOKEN', 'old-token'), \
                patch.object(g, 'load_token', return_value='new-token'), \
                patch.object(g, 'new_session', return_value=context) as new_session, \
                patch.object(g, 'inspect_token', return_value={'ok': True}) as inspect, \
                patch.object(g, 'token_age', return_value=20):
            result = g.inspect_current_token()
            new_session.assert_called_once_with('new-token')
            inspect.assert_called_once_with(session, 'new-token')
            self.assertEqual(g.TOKEN, 'new-token')
            self.assertEqual(result['age'], 20)


class Clock:
    def __init__(self, start):
        self.value = start

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.target = 10000
        self.clock = Clock(self.target - 3600)
        self.born = self.clock.now() - 100
        self.refreshes = []
        self.checks = []
        self.messages = []

    def probe(self):
        self.checks.append(self.clock.now())
        return {'ok': True, 'age': self.clock.now() - self.born,
                'http_status': 200, 'code': 200}

    def refresh(self, max_wait):
        self.refreshes.append(self.clock.now())
        self.clock.sleep(40)
        self.born = self.clock.now()
        return True

    def run_schedule(self, probe=None, refresh=None):
        return prepare_scheduled_token(
            self.target, now=self.clock.now, sleep=self.clock.sleep,
            probe=probe or self.probe, refresh=refresh or self.refresh, log=self.messages.append)

    def test_refresh_finishes_before_thirty_minutes_and_ten_minute_probe_is_real(self):
        self.assertTrue(self.run_schedule())
        self.assertEqual(self.refreshes[:2], [self.target - 33 * 60, self.target - 13 * 60])
        self.assertLessEqual(self.refreshes[0] + 40, self.target - 30 * 60)
        self.assertIn(self.target - 10 * 60, self.checks)
        self.assertIn(self.target - 60, self.checks)
        self.assertEqual(self.clock.now(), self.target - 5.5)

    def test_refresh_failure_stops_preparation(self):
        refresh = Mock(return_value=False)
        self.assertFalse(self.run_schedule(refresh=refresh))
        self.assertEqual(refresh.call_count, 1)
        self.assertTrue(any('取消' in message for message in self.messages))

    def test_refreshed_but_invalid_token_is_rejected(self):
        def invalid_after_refresh():
            result = self.probe()
            result['ok'] = not self.refreshes
            return result
        self.assertFalse(self.run_schedule(probe=invalid_after_refresh))
        self.assertEqual(len(self.refreshes), 1)

    def test_expiration_at_ten_minutes_triggers_verified_refresh(self):
        def expires_once():
            result = self.probe()
            if self.clock.now() == self.target - 600:
                result['ok'] = False
            return result
        self.assertTrue(self.run_schedule(probe=expires_once))
        self.assertIn(self.target - 600, self.refreshes)
        self.assertIn(self.target - 560, self.checks)

    def test_expiration_after_ten_minute_checkpoint_is_detected(self):
        def expires_once():
            result = self.probe()
            if self.clock.now() == self.target - 540:
                result['ok'] = False
            return result
        self.assertTrue(self.run_schedule(probe=expires_once))
        self.assertIn(self.target - 540, self.refreshes)

    def test_deadline_overrun_is_not_reported_as_success(self):
        def slow_refresh(max_wait):
            self.clock.sleep(181)
            return True
        self.assertFalse(self.run_schedule(refresh=slow_refresh))
        self.assertTrue(any('准备期限' in message for message in self.messages))

    def test_late_start_refreshes_immediately_and_checks_current_validity(self):
        self.clock.value = self.target - 300
        self.assertTrue(self.run_schedule())
        self.assertEqual(self.refreshes[0], self.target - 300)
        self.assertTrue(any('错过' in message for message in self.messages))
        self.assertIn(self.target - 260, self.checks)

    def test_insufficient_late_start_budget_aborts(self):
        self.clock.value = self.target - 70
        self.assertFalse(self.run_schedule())
        self.assertEqual(self.refreshes, [])


class GrabIntegrationTests(unittest.TestCase):
    def test_expired_token_at_preheat_cancels_instead_of_ordering(self):
        session = Mock()
        session.post.return_value = Mock(status_code=200, json=Mock(return_value={'code': 40101}))
        with patch.object(g, 'new_session', return_value=session), \
                patch.object(g, 'server_offset', return_value=0), \
                patch.object(g, 'server_now', return_value=1000), \
                patch.object(g, 'token_age', return_value=0), \
                patch.object(g, 'parse_at_target', return_value=(datetime.fromtimestamp(5000, timezone.utc), '+4000')), \
                patch('token_schedule.prepare_scheduled_token', return_value=True), \
                patch('auto_ticket.try_refresh_via_authcode', return_value=None), \
                redirect_stdout(io.StringIO()):
            self.assertFalse(g.grab(['--auto']))
        self.assertEqual([call.args[0] for call in session.post.call_args_list], [g.QUERY_URL])

    def test_failed_preparation_cannot_reach_order_endpoint(self):
        session = Mock()
        with patch.object(g, 'new_session', return_value=session), \
                patch.object(g, 'server_offset', return_value=0), \
                patch.object(g, 'server_now', return_value=1000), \
                patch.object(g, 'token_age', return_value=0), \
                patch.object(g, 'parse_at_target', return_value=(datetime.fromtimestamp(5000, timezone.utc), '+4000')), \
                patch('token_schedule.prepare_scheduled_token', return_value=False), \
                redirect_stdout(io.StringIO()):
            self.assertFalse(g.grab(['--auto']))
        session.post.assert_not_called()

    def test_dry_run_prepares_and_preheats_without_order_creation(self):
        session = Mock()
        session.post.return_value = Mock(status_code=200, json=Mock(return_value={'code': 200}))
        with patch.object(g, 'new_session', return_value=session), \
                patch.object(g, 'server_offset', return_value=0), \
                patch.object(g, 'server_now', return_value=1000), \
                patch.object(g, 'token_age', return_value=0), \
                patch.object(g, 'parse_at_target', return_value=(datetime.fromtimestamp(5000, timezone.utc), '+4000')), \
                patch('token_schedule.prepare_scheduled_token', return_value=True) as prepare, \
                patch.object(g, 'wait_until'), redirect_stdout(io.StringIO()):
            self.assertTrue(g.grab(['--auto', '--dry-run']))
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.kwargs['refresh_minutes'], 30)
        self.assertEqual(prepare.call_args.kwargs['verify_minutes'], 10)
        self.assertEqual([call.args[0] for call in session.post.call_args_list], [g.QUERY_URL])


if __name__ == '__main__':
    unittest.main()
