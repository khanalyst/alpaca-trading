from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.research_budget import check


class BudgetTests(unittest.TestCase):
    def test_window_is_identical_to_parser_and_space_does_not_imply_data_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'market-2026-08-10.csv').write_bytes(b'x'*100)
            (root/'market-2026-08-11.csv').write_bytes(b'y'*10)
            with patch('deploy.research_budget.shutil.disk_usage') as usage:
                usage.return_value.free = 1000
                r = check(None, root, 1, max_bytes=20, temporary_root=root, reserve_bytes=0)
                self.assertTrue(r['ok'])
                self.assertEqual(r['input_bytes'], 10)
                self.assertNotIn('identity', r)
                r = check(None, root, 0, max_bytes=20, temporary_root=root, reserve_bytes=0)
                self.assertEqual(r['reason'], 'input_byte_limit')
                usage.return_value.free = 79
                r = check(None, root, 1, max_bytes=20, temporary_root=root, reserve_bytes=0)
                self.assertEqual(r['reason'], 'insufficient_temporary_space')
