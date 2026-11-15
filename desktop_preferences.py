"""Persisted desktop identifiers only; no renderer, widgets or theme tokens."""
VALID_THEME_NAMES = ('modern', 'aero', 'paper', 'frutiger', 'slate')
DEFAULT_THEME_NAME = 'modern'
LEGACY_THEME_ALIASES = {'win7_aero': 'aero', 'gemini_reviewing': 'slate'}


def normalize_theme_name(value: object) -> str:
    if not isinstance(value, str):
        return DEFAULT_THEME_NAME
    normalized = LEGACY_THEME_ALIASES.get(value, value)
    return normalized if normalized in VALID_THEME_NAMES else DEFAULT_THEME_NAME
