"""Fail when files (or a commit message) contain emoji or pictographic symbols.

Usage:
    python scripts/check_no_emoji.py                 # scan the whole repository
    python scripts/check_no_emoji.py a.py docs/x.md  # scan specific files
    python scripts/check_no_emoji.py --message FILE  # scan a commit message

The project keeps all code, documentation, UI strings and commit messages free
of emoji so that output stays readable in terminals, logs and diffs.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

EMOJI_PATTERN = re.compile(
    "["
    "\U0001f000-\U0001faff"  # mahjong, cards, emoticons, symbols and pictographs, transport, etc.
    "\U00002600-\U000027bf"  # miscellaneous symbols and dingbats
    "\U00002b00-\U00002bff"  # arrows and stars used as emoji
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flags)
    "\U0000fe0f"  # variation selector-16 (emoji presentation)
    "\U0000200d"  # zero width joiner (emoji sequences)
    "\U000020e3"  # combining enclosing keycap
    "]"
)

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".ruff_cache", ".pytest_cache"}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".whl",
    ".pyc", ".so", ".dll", ".exe", ".woff", ".woff2", ".ttf", ".db", ".sqlite", ".bundle",
}


def find_emoji(text: str) -> list[tuple[int, int, str]]:
    """Return (line, column, character) for every emoji found in text."""
    hits = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in EMOJI_PATTERN.finditer(line):
            hits.append((line_no, match.start() + 1, match.group()))
    return hits


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
        return [root / p for p in out.splitlines() if p]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in root.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.parts)]


def scan(paths: list[Path]) -> int:
    failures = 0
    for path in paths:
        if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line, col, char in find_emoji(text):
            failures += 1
            print(f"{path}:{line}:{col}: emoji U+{ord(char):04X} is not allowed")
    return failures


def main(argv: list[str]) -> int:
    if argv[:1] == ["--message"]:
        text = Path(argv[1]).read_text(encoding="utf-8")
        hits = find_emoji(text)
        for line, col, char in hits:
            print(f"commit message:{line}:{col}: emoji U+{ord(char):04X} is not allowed")
        return 1 if hits else 0

    root = Path.cwd()
    paths = [Path(a) for a in argv] if argv else tracked_files(root)
    failures = scan(paths)
    if failures:
        print(f"\n{failures} emoji occurrence(s) found. Remove them before committing.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
