"""Package a frozen Windows candidate with an isolated, synthetic QA calendar."""
from argparse import ArgumentParser
from datetime import date, datetime, time, timedelta
from hashlib import file_digest
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from calendar_core import Event, EventCategory, RoutineItem
from desktop_storage_startup import StartupOptions, open_desktop_storage
from event_commands import EventCommandHistory
from version import __version__


LAUNCHER = r"""@echo off
setlocal
set "CALENDAR_QA_ROOT=%~dp0qa-profile"
set "APPDATA=%CALENDAR_QA_ROOT%\Roaming"
set "LOCALAPPDATA=%CALENDAR_QA_ROOT%\Local"
if not exist "%CALENDAR_QA_ROOT%\Calendar\storage-state.json" (
    if exist "%CALENDAR_QA_ROOT%\Calendar\" (
        echo The existing QA Calendar folder could not be verified. Use another extraction folder.
        exit /b 1
    )
    xcopy "%~dp0QA-Seed\*" "%CALENDAR_QA_ROOT%\Calendar" /E /I /H /Q
    if errorlevel 1 exit /b 1
)
if not exist "%APPDATA%" mkdir "%APPDATA%"
if not exist "%LOCALAPPDATA%" mkdir "%LOCALAPPDATA%"
start "" /D "%~dp0DesktopCalendar" "%~dp0DesktopCalendar\DesktopCalendar.exe" --isolated-data-dir "%CALENDAR_QA_ROOT%\Calendar"
"""


def seed_calendar(path, day):
    session = open_desktop_storage(StartupOptions(path))
    try:
        store = session.store
        store.upsert_category(EventCategory("qa-work", "QA 分类", "#6687F2"))
        start = datetime.combine(day, time(9, 30))
        events = [
            Event("qa-timed", "QA 今日会议", start.isoformat(), category_id="qa-work"),
            Event("qa-done", "QA 已完成事项", start.replace(hour=10).isoformat(), done=True),
            Event("qa-untimed", "QA 全天事项", start.replace(hour=23, minute=59).isoformat(), has_time=False),
            Event("qa-deadline", "QA 今日 DDL", start.replace(hour=17).isoformat(), event_type="ddl"),
            Event("qa-overdue", "QA 逾期 DDL", (start-timedelta(days=1)).isoformat(), event_type="ddl"),
            Event("qa-future", "QA 未来 DDL", (start+timedelta(days=2)).isoformat(), event_type="ddl"),
        ]
        events.extend(Event(f"qa-batch-{i}", f"QA 同分钟事项 {i}", (start+timedelta(days=1)).isoformat(),
                            reminder=0, category_id="qa-work") for i in range(1, 9))
        history = EventCommandHistory(store)
        for event in events:
            history.upsert(event)
        history.upsert_routine(RoutineItem("qa-habit", "QA 每周习惯", frequency="weekly", weekdays=[day.weekday()],
                                         created_on=day.isoformat(), workdays_only=False))
        store.repository.write_reminder_preference("native_reminders", {
            "enabled": True, "windows": False, "qq": False,
            "events": {"qa-timed": {"mode": "relative", "minutes": 15, "explicit": True}},
            "brief": {"enabled": False, "evening_enabled": False, "windows": False, "qq": False},
        })
    finally:
        session.close()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--client-commit", required=True)
    parser.add_argument("--cloud-commit", required=True)
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    for sha in (args.client_commit, args.cloud_commit):
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            parser.error("provide the full client and cloud commit hashes")
    if (args.app_dir / "app-version.txt").read_text(encoding="ascii").strip() != __version__:
        parser.error("frozen application version differs from the source")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="calendar-release-qa-") as temp:
        seed = Path(temp) / "Calendar"
        seed_calendar(seed, args.day)
        with ZipFile(args.output, "x", ZIP_DEFLATED) as package:
            for path in sorted(args.app_dir.rglob("*")):
                if path.is_file():
                    package.write(path, "DesktopCalendar/" + path.relative_to(args.app_dir).as_posix())
            for path in sorted(seed.iterdir()):
                if path.name in ("storage-state.json", ".storage-managed") or path.suffix == ".sqlite3":
                    package.write(path, "QA-Seed/" + path.name)
            package.writestr("Start-Isolated-QA.cmd", LAUNCHER.replace("\n", "\r\n"))
            package.write(ROOT/"docs"/"v0.14-windows-rc-qa.md", "QA-Manual.md")
            package.writestr("QA-Candidate.json", json.dumps({
                "version": __version__, "client_commit": args.client_commit,
                "cloud_commit": args.cloud_commit, "fixture_date": args.day.isoformat(),
            }, indent=2) + "\n")
    with ZipFile(args.output) as package:
        names = package.namelist()
        assert package.testzip() is None
        assert "DesktopCalendar/DesktopCalendar.exe" in names
        assert any(name.endswith("/DesktopCalendarNotifications.dll") for name in names)
        assert any(name.startswith("QA-Seed/") and name.endswith(".sqlite3") for name in names)
    with args.output.open("rb") as stream:
        digest = file_digest(stream, "sha256").hexdigest()
    args.output.with_suffix(".zip.sha256").write_text(f"{digest}  {args.output.name}\n", encoding="ascii")
    print(f"{digest}  {args.output}")


if __name__ == "__main__":
    main()
