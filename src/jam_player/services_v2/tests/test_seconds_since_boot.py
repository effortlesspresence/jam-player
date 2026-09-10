"""seconds_since_boot() must read /proc/uptime and fail SAFE (0.0) otherwise."""
import builtins
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.system import seconds_since_boot  # noqa: E402


class SecondsSinceBootTests(unittest.TestCase):
    def test_reads_first_field_of_proc_uptime(self):
        real_open = builtins.open

        def fake_open(path, *args, **kwargs):
            if path == '/proc/uptime':
                return io.StringIO('1234.56 4321.00\n')
            return real_open(path, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=fake_open):
            self.assertAlmostEqual(seconds_since_boot(), 1234.56)

    def test_unreadable_uptime_reports_just_booted(self):
        """0.0 keeps the recovery window OPEN when the kernel file is missing."""
        with mock.patch('builtins.open', side_effect=OSError('no /proc')):
            self.assertEqual(seconds_since_boot(), 0.0)

    def test_garbage_uptime_reports_just_booted(self):
        with mock.patch('builtins.open', return_value=io.StringIO('not a number\n')):
            self.assertEqual(seconds_since_boot(), 0.0)


if __name__ == '__main__':
    unittest.main()
