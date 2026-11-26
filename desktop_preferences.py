"""Persisted desktop identifiers only; no renderer, widgets or theme tokens."""
import os
from pathlib import Path

ACTIVE_THEME_NAMES = ('modern', 'slate')
DEFAULT_THEME_NAME = 'modern'
LEGACY_THEME_ALIASES = {'win7_aero': 'aero', 'gemini_reviewing': 'slate'}
# Glass, Paper and Frutiger Aero all have light content surfaces.
ARCHIVED_THEME_FALLBACKS = dict.fromkeys(('aero', 'paper', 'frutiger'), DEFAULT_THEME_NAME)
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


def normalize_stored_theme_name(value: object) -> str:
    """Keep recognized historical IDs in the storage and backup contract."""
    if not isinstance(value, str):
        return DEFAULT_THEME_NAME
    normalized = LEGACY_THEME_ALIASES.get(value, value)
    if normalized in ACTIVE_THEME_NAMES or normalized in ARCHIVED_THEME_FALLBACKS:
        return normalized
    return DEFAULT_THEME_NAME


def normalize_theme_name(value: object) -> str:
    """Resolve the active theme to display without rewriting saved data."""
    stored_name = normalize_stored_theme_name(value)
    return ARCHIVED_THEME_FALLBACKS.get(stored_name, stored_name)
