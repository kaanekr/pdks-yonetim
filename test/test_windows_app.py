"""HTTP client integration with disposable loopback server; NEVER a real device."""
import base64
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.client import (DeviceClient, DeviceError, canonical_uid, checked_name,
                                encode_local_time, format_local_time, normalize_address, parse_events)
from windows_app.demo import DemoClient


class Handler(BaseHTTPRequestHandler):
    calls = []
    state = {}

    def log_message(self, *args):
        pass

    def reply(self, code, data):
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def check_auth(self):
        expected = 'Basic ' + base64.b64encode(b'admin:test-pass').decode()
        if self.headers.get('Authorization') != expected:
            self.reply(401, {'ok': False})
            return False
        return True

    def do_GET(self):
        Handler.calls.append(('GET', self.path))
        if not self.check_auth():
            return
        if self.path == '/api/status':
            if Handler.state.get('redirect'):
                self.send_response(302)
                self.send_header('Location', '/redirected')
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                self.reply(200, Handler.state)
        elif self.path == '/api/events.csv':
            self.reply(200, b'sequence,epoch,uid,event,seconds\n1,1800000000,11223344,I,0\n2,1750000000,00000000,R,1800000000\n')
        else:
            self.reply(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        Handler.calls.append(('POST', self.path, body, self.headers.get('X-CSRF-Token')))
        if not self.check_auth():
            return
        if self.headers.get('X-CSRF-Token') != Handler.state['csrf']:
            self.reply(403, {'message': 'CSRF RED'})
        elif body.get('expected_sequence', Handler.state['sequence']) != Handler.state['sequence']:
            self.reply(409, {'message': 'DURUM DEGISTI'})
        else:
            self.reply(200, {'ok': True, 'message': 'fixture only'})


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        Handler.calls = []
        Handler.state = DemoClient().status()
        self.client = DeviceClient(self.url, 'admin', 'test-pass', timeout=1)

    def test_status_has_no_write_and_recovery_explicit_capability(self):
        state = self.client.status()
        self.assertTrue(state['clock_recovery'])
        self.assertEqual(Handler.calls, [('GET', '/api/status')])

    def test_wrong_password_does_not_leak_password(self):
        with self.assertRaises(DeviceError) as caught:
            DeviceClient(self.url, 'admin', 'wrong-private-secret').status()
        self.assertIn('401', str(caught.exception))
        self.assertNotIn('wrong-private-secret', str(caught.exception))

    def test_personnel_payload_keeps_stable_identity_when_card_changes(self):
        state = self.client.status()
        self.client.save_employee(state, '11223344', 'Özge', 'AABBCCDD', True, False)
        call = Handler.calls[-1]
        self.assertEqual(call[1], '/api/employee/update')
        self.assertEqual(call[2]['uid'], '11223344')
        self.assertEqual(call[2]['card_uid'], 'AABBCCDD')
        self.assertEqual(call[2]['roster_version'], state['roster_version'])
        self.assertEqual(call[3], state['csrf'])

    def test_recovery_is_single_post_with_confirmation_and_version(self):
        state = self.client.status()
        self.client.set_time(state, '2026-11-01 12:34:56', True)
        call = Handler.calls[-1]
        self.assertEqual(call[1], '/api/time/recover')
        self.assertEqual(call[2]['confirm'], 'IPTAL VE SAAT KURTAR')
        self.assertEqual(call[2]['expected_sequence'], state['sequence'])
        self.assertEqual(sum(c[0] == 'POST' for c in Handler.calls), 1)

    def test_stale_write_is_not_retried(self):
        state = self.client.status()
        Handler.state['sequence'] += 1
        with self.assertRaisesRegex(DeviceError, '409'):
            self.client.set_time(state, '2026-11-01 12:34:56', True)
        self.assertEqual(sum(c[0] == 'POST' for c in Handler.calls), 1)

    def test_normal_time_does_not_send_recovery_flag(self):
        self.client.set_time(self.client.status(), '2026-11-01 12:34:56')
        self.assertEqual(Handler.calls[-1][1], '/api/time')
        self.assertEqual(set(Handler.calls[-1][2]), {'epoch'})

    def test_legacy_firmware_blocks_new_features_but_rename_works(self):
        state = self.client.status()
        state.pop('clock_recovery')
        state.pop('personnel_management')
        with self.assertRaises(DeviceError):
            self.client.set_time(state, '2026-11-01 12:34:56', True)
        with self.assertRaises(DeviceError):
            self.client.save_employee(state, '11223344', 'Name', 'AABBCCDD')
        self.client.save_employee(state, '11223344', 'Name', '11223344')
        self.assertEqual(Handler.calls[-1][1], '/api/employee')
        self.assertEqual(sum(c[0] == 'POST' for c in Handler.calls), 1)

    def test_duplicate_create_is_not_accidental_legacy_rename(self):
        with self.assertRaises(DeviceError):
            self.client.save_employee(self.client.status(), '11223344', 'Name', '11223344', create=True)
        self.assertFalse(any(c[0] == 'POST' for c in Handler.calls))

    def test_csv_recovery_old_epoch_is_not_added_as_worked_duration(self):
        data = self.client.events()
        rows = parse_events(data)
        self.assertIn('saat kurtarma', rows[1][3])
        self.assertIn('Önceki saat', rows[1][4])
        self.assertNotIn('500000:', rows[1][4])

    def test_local_epoch_does_not_depend_on_windows_timezone(self):
        self.assertEqual(encode_local_time('2024-01-01 00:00:00'), 1704067200)
        self.assertEqual(format_local_time(1704067200), '2024-01-01 00:00:00')
        for bad in ('2023-12-31 23:59:59', '2100-01-01 00:00:00', '2026-02-30 00:00:00'):
            with self.assertRaises(DeviceError):
                encode_local_time(bad)

    def test_address_validation_and_proxy_redirect_safety(self):
        self.assertEqual(normalize_address('192.168.4.1'), 'http://192.168.4.1')
        for bad in ('file:///etc/passwd', 'http://user:pass@localhost', 'http://localhost/wifi',
                    'http://localhost/?x=1', 'http://localhost/#x', 'http://local host', 'http://localhost:99999'):
            with self.assertRaises(DeviceError):
                normalize_address(bad)

    def test_http_redirect_is_not_followed(self):
        Handler.state['redirect'] = True
        with self.assertRaisesRegex(DeviceError, '302'):
            self.client.status()
        self.assertEqual(Handler.calls, [('GET', '/api/status')])

    def test_oversized_status_is_rejected(self):
        Handler.state['message'] = 'x' * (256 * 1024)
        with self.assertRaisesRegex(DeviceError, 'boyut'):
            self.client.status()

    def test_missing_csrf_never_sends_post(self):
        state = self.client.status()
        state['csrf'] = ''
        with self.assertRaisesRegex(DeviceError, 'CSRF'):
            self.client.set_time(state, '2026-11-01 12:34:56')
        self.assertFalse(any(c[0] == 'POST' for c in Handler.calls))

    def test_input_limits_utf8_and_uid(self):
        self.assertEqual(canonical_uid('aabbccdd'), 'AABBCCDD')
        self.assertEqual(checked_name(' Özge '), 'Özge')
        for name in ('', ' ' * 3, '\x00bad', 'ğ' * 25):
            with self.assertRaises(DeviceError):
                checked_name(name)
        for uid in ('123', 'A' * 21, 'XXXXXXXX', 'AABB CCDD'):
            with self.assertRaises(DeviceError):
                canonical_uid(uid)

    def test_demo_recovery_preserves_completed_totals_and_adds_R(self):
        demo = DemoClient()
        state = demo.status()
        demo.set_time(state, '2026-11-01 12:34:56', True)
        after = demo.status()
        self.assertFalse(after['employees'][0]['inside'])
        self.assertEqual(after['employees'][0]['total_seconds'], 3600)
        self.assertIn(b',R,', demo.events())


if __name__ == '__main__':
    unittest.main(verbosity=2)
