from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from version import __version__


GITHUB_REPOSITORY = "AstroloNeko/desktop-calendar-widget"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
RELEASE_ASSET_NAME = "DesktopCalendar-win64.zip"
CHECKSUM_ASSET_NAME = RELEASE_ASSET_NAME + ".sha256"
USER_AGENT = f"DesktopCalendar/{__version__}"


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    tag: str
    version: str
    name: str
    notes: str
    release_url: str
    asset_url: str
    checksum_url: str
    asset_size: int = 0


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if not match:
        raise UpdateError(f"无法识别版本号：{value}")
    return tuple(int(part or 0) for part in match.groups())


def is_newer_version(candidate: str, current: str = __version__) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def parse_release(payload: dict) -> UpdateInfo:
    tag = str(payload.get("tag_name", "")).strip()
    if not tag:
        raise UpdateError("GitHub Release 缺少版本号。")
    assets = payload.get("assets", [])
    if not isinstance(assets, list):
        raise UpdateError("GitHub Release 的附件信息不完整。")
    by_name = {str(item.get("name", "")): item for item in assets if isinstance(item, dict)}
    archive = by_name.get(RELEASE_ASSET_NAME)
    checksum = by_name.get(CHECKSUM_ASSET_NAME)
    if not archive:
        raise UpdateError(f"最新 Release 中没有 {RELEASE_ASSET_NAME}。")
    if not checksum:
        raise UpdateError(f"最新 Release 中没有 SHA-256 校验文件，已拒绝自动安装。")
    asset_url = str(archive.get("browser_download_url", ""))
    checksum_url = str(checksum.get("browser_download_url", ""))
    if not asset_url or not checksum_url:
        raise UpdateError("Release 附件没有可用的下载地址。")
    version = tag[1:] if tag.lower().startswith("v") else tag
    return UpdateInfo(
        tag=tag,
        version=version,
        name=str(payload.get("name") or tag),
        notes=str(payload.get("body") or ""),
        release_url=str(payload.get("html_url") or f"https://github.com/{GITHUB_REPOSITORY}/releases/tag/{tag}"),
        asset_url=asset_url,
        checksum_url=checksum_url,
        asset_size=int(archive.get("size") or 0),
    )


def _request(url: str, timeout: int = 20):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return urllib.request.urlopen(request, timeout=timeout)


def check_for_update(timeout: int = 15) -> UpdateInfo:
    try:
        with _request(LATEST_RELEASE_API, timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("暂时没有可用的正式版本。") from exc
        raise UpdateError(f"GitHub 返回错误：HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"无法连接 GitHub：{exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("GitHub 返回了无法解析的数据。") from exc
    return parse_release(payload)


def _read_checksum(url: str) -> str:
    try:
        with _request(url, 20) as response:
            text = response.read(4096).decode("ascii", errors="ignore")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"无法下载校验文件：{exc}") from exc
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not match:
        raise UpdateError("Release 的 SHA-256 校验文件格式不正确。")
    return match.group(1).lower()


def verify_archive(archive: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest().lower() != expected_sha256.lower():
        raise UpdateError("更新包校验失败，文件可能不完整，已取消安装。")


def download_update(info: UpdateInfo, progress: Optional[Callable[[int], None]] = None) -> Path:
    expected = _read_checksum(info.checksum_url)
    staging = Path(tempfile.gettempdir()) / "DesktopCalendarUpdate"
    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / RELEASE_ASSET_NAME
    temporary = archive.with_suffix(".download")
    try:
        with _request(info.asset_url, 60) as response, temporary.open("wb") as output:
            total = int(response.headers.get("Content-Length") or info.asset_size or 0)
            downloaded = 0
            while True:
                block = response.read(256 * 1024)
                if not block:
                    break
                output.write(block)
                downloaded += len(block)
                if progress and total:
                    progress(min(99, int(downloaded * 100 / total)))
        temporary.replace(archive)
        verify_archive(archive, expected)
        if progress:
            progress(100)
        return archive
    except UpdateError:
        temporary.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"更新包下载失败：{exc}") from exc


def running_as_packaged_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def launch_updater(archive: Path) -> None:
    if not running_as_packaged_app():
        raise UpdateError("源码运行模式不能自动覆盖安装；请在 GitHub Release 中下载发布版。")
    install_dir = Path(sys.executable).resolve().parent
    bundled_updater = install_dir / "DesktopCalendarUpdater.exe"
    if not bundled_updater.exists():
        raise UpdateError("安装目录中缺少 DesktopCalendarUpdater.exe。")
    staging = Path(tempfile.gettempdir()) / "DesktopCalendarUpdate"
    staging.mkdir(parents=True, exist_ok=True)
    staged_updater = staging / "DesktopCalendarUpdater.exe"
    shutil.copy2(bundled_updater, staged_updater)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [
            str(staged_updater),
            "--archive",
            str(archive),
            "--install-dir",
            str(install_dir),
            "--pid",
            str(os.getpid()),
            "--executable",
            Path(sys.executable).name,
        ],
        cwd=str(staging),
        close_fds=True,
        creationflags=flags,
    )
