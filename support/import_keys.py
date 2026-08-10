from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def import_provider_keys(source: Path, destination: Path) -> int:
    entries: list[tuple[str, str]] = []
    private_settings: list[tuple[str, str]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        name, separator, key = line.partition(":")
        name = name.strip()
        key = key.strip()
        if separator and name in {"upstream_url", "forced_model"} and key:
            private_settings.append((name, key))
        elif separator and name and key.startswith("sk-"):
            entries.append((name, key))
    if not entries:
        raise SystemExit("未找到可迁移的 Provider 密钥")
    if len({name for name, _ in entries}) != len(entries):
        raise SystemExit("检测到重复的渠道名称，已停止迁移")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, delete=False
    ) as handle:
        for name, value in private_settings:
            handle.write(f"{name}:{value}\n")
        for name, key in entries:
            handle.write(f"{name}:{key}\n")
        temp_name = handle.name
    os.chmod(temp_name, 0o600)
    os.replace(temp_name, destination)
    return len(entries)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: import_keys.py SOURCE DESTINATION")
    count = import_provider_keys(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"已迁移 {count} 个渠道，密钥内容未输出")
