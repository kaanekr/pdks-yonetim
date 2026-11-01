"""Packaged-acceptance fixture also exercised from source without real hardware."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_app.deletion_smoke import run_deletion_smoke


class DeletionSmokeTest(unittest.TestCase):
    def test_named_history_deletion_reopen_and_exports(self):
        with tempfile.TemporaryDirectory(dir=r'C:\Users\PC\AppData\Local\Temp\opencode') as folder:
            path = Path(folder)/'acceptance.json'
            result = run_deletion_smoke(path)
            self.assertTrue(result['ok'])
            self.assertFalse(result['device_contacted'])
            self.assertFalse(result['firmware_uploaded'])
            self.assertTrue(path.with_suffix('.pdf').is_file())
            self.assertTrue(path.with_suffix('.xlsx').is_file())


if __name__ == '__main__':
    unittest.main(verbosity=2)
