"""Run selected offline unittest checks or explicitly authorized Windows QA."""
import argparse
from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_SCRIPTS = frozenset('''
    benchmark_ui_polish check_backup_gui check_backup_recovery check_batch_delete check_bulk_actions
    check_calendar_chat check_desktop_storage check_direct_schema8_exe
    check_final_visuals check_frameless_global check_global_hotkey check_global_sidebar
    check_habit_schedules check_interactions check_mutation_transactions
    check_native_notifications check_native_reminder_app check_rc_updater_gate
    check_release_history check_reminder_center check_reminder_ef_native check_reminder_ui
    check_reminder_ui_special check_reminder_v0140 check_reminder_visuals
    check_secondary_surfaces check_sqlite_rc_exe check_storage_upgrade_button
    check_system_tray check_update_compatibility check_update_dialog check_v092_visuals
    check_window_drag
'''.split())


@contextmanager
def isolated_profile():
    with TemporaryDirectory(prefix='desktop-calendar-tests-') as directory:
        root = Path(directory)
        roaming, local = root / 'Roaming', root / 'Local'
        roaming.mkdir()
        local.mkdir()
        with patch.dict(os.environ, APPDATA=str(roaming), LOCALAPPDATA=str(local)):
            yield


@contextmanager
def block_tk_windows():
    import tkinter

    def reject_window(_root, *args, **kwargs):
        raise AssertionError('Real Tk roots are forbidden offline. Mock Tk or move native checks to L4.')

    with patch.object(tkinter.Tk, '__init__', reject_window):
        yield


def test_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_cases(item)
        else:
            yield item


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('-p', '--pattern', action='append', metavar='GLOB', help='test file glob; repeat for module regression')
    target.add_argument('--all', action='store_true', help='full offline regression')
    target.add_argument('--windows', action='store_true', help='L4 native widget tests')
    target.add_argument('--desktop-script', choices=sorted(DESKTOP_SCRIPTS), metavar='NAME',
                        help='one L4 script from tools/, without .py; arguments follow --')
    parser.add_argument('-k', '--match', action='append', metavar='NAME', help='filter by unittest test name')
    parser.add_argument('--list', action='store_true', help='discover and print test IDs without running them')
    parser.add_argument('--allow-desktop', action='store_true', help='acknowledge visible windows and native desktop effects')
    parser.add_argument('-v', '--verbose', action='store_true')
    command = list(sys.argv[1:] if argv is None else argv)
    script_args = []
    if '--' in command:
        separator = command.index('--')
        command, script_args = command[:separator], command[separator + 1:]
    args = parser.parse_args(command)
    if script_args and not args.desktop_script:
        parser.error('arguments after -- require --desktop-script')
    if args.desktop_script == 'check_storage_upgrade_button' and '--manual-exe' in script_args:
        parser.error('--manual-exe starts a persistent session; use the separate manual acceptance procedure')
    if args.desktop_script and (args.list or args.match or args.verbose):
        parser.error('pass script-specific options after --')
    interactive = bool(args.desktop_script or (args.windows and not args.list))
    if interactive and not args.allow_desktop:
        parser.error('Windows acceptance can open windows or issue native input; requires --allow-desktop')
    if args.allow_desktop and not interactive:
        parser.error('--allow-desktop only applies when running Windows acceptance')
    if interactive and sys.platform != 'win32':
        parser.error('Windows acceptance requires Windows')

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    with isolated_profile():
        if args.desktop_script:
            script = ROOT / 'tools' / (args.desktop_script + '.py')
            return subprocess.run([sys.executable, str(script), *script_args], cwd=ROOT).returncode
        with (nullcontext() if interactive else block_tk_windows()):
            start = ROOT / ('tools/windows_tests' if args.windows else 'tests')
            patterns = args.pattern or ['test_*.py']
            loader = unittest.TestLoader()
            if args.match:
                loader.testNamePatterns = [name if '*' in name else f'*{name}*' for name in args.match]
            cases = {}
            for pattern in patterns:
                for case in test_cases(loader.discover(str(start), pattern=pattern)):
                    cases.setdefault(case.id(), case)
            if args.list:
                for name in cases:
                    print(name)
                for error in loader.errors:
                    print(error, file=sys.stderr)
                if loader.errors:
                    return 1
                if not cases:
                    parser.error('no tests matched the selection')
                return 0
            if not cases:
                parser.error('no tests matched the selection')
            result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(unittest.TestSuite(cases.values()))
            return 0 if result.wasSuccessful() and not loader.errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
