"""Exercise the test CLI against a disposable unittest tree."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tools import run_tests


class TestRunnerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for lane in ('tests', 'tools/windows_tests'):
            (self.root / lane).mkdir(parents=True)
        (self.root / 'tools/windows_tests/test_windows_qa.py').write_text('''
import unittest
class DesktopChecks(unittest.TestCase):
    def test_native(self):
        raise AssertionError("fixture must never be executed by offline runs")
''', encoding='utf-8')
        (self.root / 'tests/test_scope.py').write_text('''
import os
from pathlib import Path
import tkinter
import unittest
from unittest.mock import patch

class OfflineChecks(unittest.TestCase):
    def test_profile_and_guard(self):
        with patch('tkinter._tkinter.create', side_effect=AssertionError('native Tk reached')) as create:
            with self.assertRaisesRegex(AssertionError, 'forbidden offline'):
                tkinter.Tk()
            create.assert_not_called()
        for variable in ('APPDATA', 'LOCALAPPDATA'):
            profile = Path(os.environ[variable])
            self.assertTrue(profile.is_dir())
            self.assertNotEqual(profile, Path(__file__).resolve().parent.parent)
            (profile / 'fixture').touch()
            Path(__file__).with_name(variable).write_text(str(profile), encoding='utf-8')

    def test_failure(self):
        self.fail('synthetic failure')
''', encoding='utf-8')

    def invoke(self, *args):
        script = ('import sys; from pathlib import Path; from tools import run_tests; '
                  'run_tests.ROOT = Path(sys.argv[1]); raise SystemExit(run_tests.main(sys.argv[2:]))')
        env = dict(os.environ, APPDATA=str(self.root), LOCALAPPDATA=str(self.root))
        return subprocess.run([sys.executable, '-B', '-c', script, str(self.root), *args],
                              cwd=run_tests.ROOT, env=env, capture_output=True, text=True, timeout=20)

    def test_target_filter_and_profile_cleanup(self):
        result = self.invoke('-p', 'test_scope.py', '-k', 'test_profile_and_guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ran 1 test', result.stderr)
        for key in ('APPDATA', 'LOCALAPPDATA'):
            saved = (self.root / 'tests' / key).read_text(encoding='utf-8')
            self.assertFalse(Path(saved).parent.exists())

    def test_default_discovery_excludes_windows_and_does_not_run_cases(self):
        result = self.invoke('--all', '--list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('test_scope.OfflineChecks.test_failure', result.stdout)
        self.assertNotIn('test_windows_qa', result.stdout)
        self.assertFalse((self.root / 'tests/APPDATA').exists())

    def test_failure_returns_nonzero(self):
        result = self.invoke('-p', 'test_scope.py', '-k', 'test_failure')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('synthetic failure', result.stderr)

    def test_windows_requires_explicit_opt_in(self):
        for args in (('--windows',), ('--desktop-script', 'check_frameless_global'),
                     ('--desktop-script', 'check_reminder_v0140'),
                     ('--desktop-script', 'check_reminder_ef_native'),
                     ('--desktop-script', 'check_reminder_ui_special')):
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('requires --allow-desktop', result.stderr)
