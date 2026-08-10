from __future__ import annotations

import re
import sys
from pathlib import Path


def update(path: Path, base_url: str) -> None:
    text = path.read_text(encoding="utf-8")
    section = re.compile(
        r"(?ms)(^\[model_providers\.fox\]\s*\n)(.*?)(?=^\[|\Z)"
    )

    def replace(match: re.Match[str]) -> str:
        body = match.group(2)
        line = f'base_url = "{base_url}"'
        if re.search(r"^base_url\s*=", body, flags=re.MULTILINE):
            body = re.sub(r"^base_url\s*=.*$", line, body, count=1, flags=re.MULTILINE)
        else:
            body = line + "\n" + body
        return match.group(1) + body

    updated, count = section.subn(replace, text, count=1)
    if count != 1:
        raise SystemExit("[model_providers.fox] section not found")
    path.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: migrate_codex.py CONFIG_PATH BASE_URL")
    update(Path(sys.argv[1]), sys.argv[2])
