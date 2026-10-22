"""Stage only the updater supplied by an already checksum-verified release."""
from pathlib import Path
import shutil
import tempfile
import zipfile


class PayloadError(RuntimeError):
    pass


def stage_verified_updater(archive: Path) -> Path:
    app_member = "DesktopCalendar/DesktopCalendar.exe"
    updater_member = "DesktopCalendar/DesktopCalendarUpdater.exe"
    try:
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
            if names.count(app_member) != 1 or names.count(updater_member) != 1:
                raise PayloadError("发布包缺少主程序或更新器，或含重复程序文件。")
            if not any(name.startswith("DesktopCalendar/_internal/") for name in names):
                raise PayloadError("发布包缺少必要运行文件。")
            if package.testzip() is not None:
                raise PayloadError("发布包损坏，已取消安装。")
            member = package.getinfo(updater_member)
            if member.is_dir() or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise PayloadError("发布包中的更新器无效。")
            staging = Path(tempfile.mkdtemp(prefix="DesktopCalendarUpdater-"))
            target = staging / "DesktopCalendarUpdater.exe"
            try:
                with package.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                with target.open("rb") as executable:
                    if executable.read(2) != b"MZ":
                        raise PayloadError("发布包中的更新器不是 Windows 可执行文件。")
                return target
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise PayloadError(f"无法读取已下载的发布包：{exc}") from exc
