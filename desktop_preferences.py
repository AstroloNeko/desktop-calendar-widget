"""Persisted desktop identifiers only; no renderer, widgets or theme tokens."""
import os
from pathlib import Path

VALID_THEME_NAMES = ('modern', 'aero', 'paper', 'frutiger', 'slate')
DEFAULT_THEME_NAME = 'modern'
LEGACY_THEME_ALIASES = {'win7_aero': 'aero', 'gemini_reviewing': 'slate'}
SQLITE_UPGRADE_DECLINED = '.sqlite-upgrade-declined'


def sqlite_upgrade_declined(directory):
    marker = Path(directory) / SQLITE_UPGRADE_DECLINED
    return not marker.is_symlink() and marker.is_file()


def decline_sqlite_upgrade(directory):
    """Local device preference, deliberately outside calendar/backup snapshots."""
    marker = Path(directory) / SQLITE_UPGRADE_DECLINED
    try:
        with marker.open('xb') as stream:
            os.fsync(stream.fileno())
    except FileExistsError:
        if not sqlite_upgrade_declined(directory):
            raise


def normalize_theme_name(value: object) -> str:
    if not isinstance(value, str):
        return DEFAULT_THEME_NAME
    normalized = LEGACY_THEME_ALIASES.get(value, value)
    return normalized if normalized in VALID_THEME_NAMES else DEFAULT_THEME_NAME
