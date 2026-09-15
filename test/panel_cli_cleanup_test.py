#!/usr/bin/env python3
"""Exercise gray diagnostic CLI cleanup with failed checks and END requests."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import papermono_gray_check as checker


class PanelCliCleanupTest(unittest.TestCase):
    def run_main(self, check_error=None, end_error=None):
        port = mock.Mock()
        port.open.side_effect = lambda: self.assertEqual((port.dtr, port.rts), (True, True))
        link = mock.Mock()
        link.request.return_value = ["GRAYTEST", "END"]
        link.request.side_effect = end_error
        serial = SimpleNamespace(Serial=mock.Mock(return_value=port))
        stdout, stderr = StringIO(), StringIO()
        failure = None
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with mock.patch.dict(sys.modules, {"serial": serial}), \
                    mock.patch.object(sys, "argv", ["papermono_gray_check.py", "--port", "fake",
                                                   "--output", str(output)]), \
                    mock.patch.object(checker, "GrayLink", return_value=link) as link_type, \
                    mock.patch.object(checker, "run_checks", side_effect=check_error) as checks, \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    checker.main()
                except checker.ProtocolError as error:
                    failure = error
            checks.assert_called_once_with(link, output, "rotated")
            link.request.assert_called_once_with("CMD:GRAYTEST END")
            port.open.assert_called_once_with()
            port.close.assert_called_once_with()
            self.assertTrue(link_type.call_args.args[1].closed)
        return failure, stdout.getvalue(), stderr.getvalue()

    def test_check_failure_remains_primary_when_end_fails(self):
        check_error = checker.ProtocolError("capture CRC mismatch")
        end_error = checker.ProtocolError("END release timed out")
        failure, stdout, stderr = self.run_main(check_error, end_error)
        self.assertIs(failure, check_error)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "FAIL diagnostic cleanup: END release timed out\n")

    def test_end_failure_fails_successful_checks(self):
        end_error = checker.ProtocolError("END release timed out")
        failure, stdout, stderr = self.run_main(end_error=end_error)
        self.assertIs(failure, end_error)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_check_failure_survives_successful_end(self):
        check_error = checker.ProtocolError("capture CRC mismatch")
        failure, stdout, stderr = self.run_main(check_error=check_error)
        self.assertIs(failure, check_error)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_successful_checks_and_end_pass(self):
        failure, stdout, stderr = self.run_main()
        self.assertIsNone(failure)
        self.assertTrue(stdout.startswith("PASS gray capture and BW transitions; evidence: "))
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
