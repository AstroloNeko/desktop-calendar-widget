"""Offline release history and inert Markdown text; no UI or user-data access."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class NoteBlock:
    text: str
    style: str = "body"


def inline_text(text: str) -> str:
    parts = re.split(r"(`+[^`\n]*`+)", text)
    for index, part in enumerate(parts):
        if part.startswith("`") and part.endswith("`"):
            parts[index] = part.strip("`")
        else:
            part = re.sub(r"!?\[([^\]]*)\]\(([^)]*)\)", r"\1 (\2)", part)
            part = re.sub(r"(\*\*|__)(.+?)\1", r"\2", part)
            parts[index] = re.sub(r"\\([\\`*_{}\[\]()#+.!>-])", r"\1", part)
    return "".join(parts)


def release_note_blocks(notes: str) -> tuple[NoteBlock, ...]:
    """Retain the complete document; Markdown/HTML never executes anything."""
    if not notes.strip():
        return (NoteBlock("这个版本没有附加说明。"),)
    blocks = []
    fenced = False
    for line in notes.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if fenced:
            blocks.append(NoteBlock(line, "code"))
            continue
        heading = re.match(r"^\s{0,3}(#{1,3})\s+(.+?)(?:\s+#+)?$", line)
        if heading:
            blocks.append(NoteBlock(inline_text(heading[2]), f"h{len(heading[1])}"))
            continue
        bullet = re.match(r"^(\s*)[-+*]\s+(.+)$", line)
        if bullet:
            line = bullet[1] + "• " + bullet[2]
        blocks.append(NoteBlock(inline_text(line)))
    return tuple(blocks)


@dataclass(frozen=True)
class ReleaseEntry:
    version: str
    title: str
    notes: str


def parse_history(text: str) -> tuple[ReleaseEntry, ...]:
    """Split only version-level headings, ignoring examples inside code fences."""
    entries = []
    current = None
    lines = []
    fenced = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        match = None if fenced else re.match(r"^##\s+v?(\d+\.\d+\.\d+)(?:\s.*)?$", line)
        if match:
            if current:
                entries.append(ReleaseEntry(*current, "\n".join(lines).strip()))
            current = (match[1], line.removeprefix("## ").strip())
            lines = []
        elif current:
            lines.append(line)
    if current:
        entries.append(ReleaseEntry(*current, "\n".join(lines).strip()))
    # Preserve the file's curated order; never merge unrelated release bodies.
    return tuple(dict((entry.version, entry) for entry in entries).values())


def load_history(path: Path, current_version: str) -> tuple[ReleaseEntry, ...]:
    try:
        entries = parse_history(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError):
        entries = ()
    if not any(entry.version == current_version for entry in entries):
        entries = (ReleaseEntry(current_version, f"v{current_version}",
                               "本地没有此版本的更新记录。可使用“检查更新”查看 GitHub 正式版本说明。"), *entries)
    return entries
