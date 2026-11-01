"""Manual-attendance client integration on a disposable loopback fixture only."""
from pathlib import Path
import sys
from http.server import ThreadingHTTPServer
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_windows_app import Handler
from windows_app.client import DeviceClient,DeviceError,encode_local_time,parse_events
from windows_app.demo import DemoClient


class ManualClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=3)

    def setUp(self):
        Handler.calls=[]
        Handler.state=DemoClient().status()
        Handler.state.update(clock_ok=True,now=1800000000)
        Handler.state['employees'][0].update(inside=False,card_uid='AABBCCDD',active=False)
        self.client=DeviceClient(self.url,'admin','test-pass',timeout=1)
        self.state=self.client.status()

    def add(self,start='2026-10-31 09:00:00',end='2026-10-31 10:00:00',uid='11223344'):
        return self.client.add_manual(self.state,uid,start,end)

    def assert_no_post(self):
        self.assertFalse(any(c[0]=='POST' for c in Handler.calls))

    def test_explicit_confirm_version_and_stable_identity_payload(self):
        self.add()
        call=Handler.calls[-1]
        self.assertEqual(call[1],'/api/attendance/manual')
        self.assertEqual(call[2]['uid'],'11223344')
        self.assertEqual(call[2]['confirm'],'MANUEL MESAI EKLE')
        self.assertEqual(call[2]['end_epoch']-call[2]['start_epoch'],3600)
        self.assertNotIn('seconds',call[2]) # server derives duration
        self.assertEqual(call[2]['expected_sequence'],self.state['sequence'])
        self.assertEqual(call[3],self.state['csrf'])

    def test_legacy_feature_gate_prevents_post(self):
        self.state.pop('manual_attendance')
        with self.assertRaisesRegex(DeviceError,'firmware'):
            self.add()
        self.assert_no_post()

    def test_invalid_or_reverse_ranges_never_post(self):
        for start,end in (('bad','2026-10-31 10:00:00'),('2026-10-31 10:00:00','2026-10-31 09:00:00'),('2026-10-31 10:00:00','2026-10-31 10:00:00')):
            with self.assertRaises(DeviceError):
                self.add(start,end)
        self.assert_no_post()

    def test_clock_invalid_or_open_employee_never_post(self):
        self.state['clock_ok']=False
        with self.assertRaises(DeviceError): self.add()
        self.state['clock_ok']=True
        self.state['employees'][0]['inside']=True
        with self.assertRaises(DeviceError): self.add()
        self.assert_no_post()

    def test_current_card_is_not_mistaken_for_stable_identity(self):
        with self.assertRaises(DeviceError): self.add(uid='AABBCCDD')
        self.assert_no_post()

    def test_stale_rejected_once_without_retry(self):
        Handler.state['sequence']+=1
        with self.assertRaisesRegex(DeviceError,'409'): self.add()
        self.assertEqual(sum(c[0]=='POST' for c in Handler.calls),1)

    def test_csv_manual_row_displays_start_and_duration(self):
        end=encode_local_time('2026-10-31 10:00:00')
        rows=parse_events(f'sequence,epoch,uid,event,seconds\n5,{end},11223344,M,3600\n'.encode())
        self.assertIn('Manuel mesai',rows[0][3])
        self.assertIn('2026-10-31 09:00:00',rows[0][3])
        self.assertEqual(rows[0][4],'1:00:00')
        with self.assertRaises(DeviceError):
            parse_events(f'sequence,epoch,uid,event,seconds\n5,{end},11223344,M,0\n'.encode())

    def test_demo_duplicate_overlap_adjacency_and_totals(self):
        demo=DemoClient()
        demo.state.update(clock_ok=True,now=1800000000)
        demo.state['employees'][0]['inside']=False
        demo.add_manual(demo.status(),'11223344','2026-10-31 09:00:00','2026-10-31 10:00:00')
        before=demo.status()
        for start,end in (('2026-10-31 09:00:00','2026-10-31 10:00:00'),('2026-10-31 09:30:00','2026-10-31 10:30:00')):
            with self.assertRaises(DeviceError): demo.add_manual(demo.status(),'11223344',start,end)
        self.assertEqual(demo.status(),before)
        demo.add_manual(demo.status(),'11223344','2026-10-31 10:00:00','2026-10-31 11:00:00')
        self.assertEqual(demo.status()['employees'][0]['total_seconds'],10800)
        self.assertEqual(demo.status()['last_event_epoch'],before['last_event_epoch'])


if __name__=='__main__':
    unittest.main(verbosity=2)
