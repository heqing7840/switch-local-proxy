from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


FORBIDDEN_PATH_PARTS = {
    "key.txt",
    ".env",
    "runtime",
    "output",
    ".playwright-cli",
}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log"}
ALLOWED_URL_HOSTS = {
    "127.0.0.1",
    "localhost",
    "github.com",
    "code.claude.com",
    "platform.claude.com",
    "unlicense.org",
    "www.w3.org",
}
PLACEHOLDER_WORDS = {
    "example",
    "fake",
    "local-only",
    "placeholder",
    "provider",
    "test",
    "upstream",
}
SECRET_PATTERNS = {
    "provider_key": re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}"),
    "github_token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"
    ),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "absolute_user_path": re.compile(
        r"/(?:" + "Users" + r"|home)/[A-Za-z0-9._-]+/[^\s\"']*"
    ),
    "email_address": re.compile(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
    ),
}
URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]`\"']+")


def git(root: Path, *args: str, text: bool = False) -> bytes | str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=text, stderr=subprocess.DEVNULL
    )


def path_is_forbidden(path: str) -> bool:
    parts = set(Path(path).parts)
    return bool(parts & FORBIDDEN_PATH_PARTS) or Path(path).suffix.lower() in FORBIDDEN_SUFFIXES


def scan_text(path: str, data: bytes, ref: str) -> list[tuple[str, str, int, str]]:
    if b"\0" in data:
        return []
    text = data.decode("utf-8", errors="replace")
    findings: list[tuple[str, str, int, str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for category, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(line):
                value = match.group(0).lower()
                if category == "provider_key" and any(
                    word in value for word in PLACEHOLDER_WORDS
                ):
                    continue
                findings.append((category, path, line_number, ref))
        for match in URL_PATTERN.finditer(line):
            hostname = (urlsplit(match.group(0)).hostname or "").lower()
            if (
                hostname
                and hostname not in ALLOWED_URL_HOSTS
                and hostname != "example.com"
                and not hostname.endswith(".example.com")
                and not hostname.endswith(".invalid")
                and not hostname.endswith(".example")
            ):
                findings.append(("unapproved_url", path, line_number, ref))
    return findings


def tracked_findings(root: Path) -> list[tuple[str, str, int, str]]:
    paths = [
        item.decode("utf-8", errors="replace")
        for item in git(
            root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
        ).split(b"\0")
        if item
    ]
    findings: list[tuple[str, str, int, str]] = []
    for path in paths:
        if path_is_forbidden(path):
            findings.append(("forbidden_tracked_path", path, 0, "working-tree"))
            continue
        findings.extend(scan_text(path, (root / path).read_bytes(), "working-tree"))
    return findings


def history_findings(root: Path) -> list[tuple[str, str, int, str]]:
    commits = str(git(root, "rev-list", "--all", text=True)).split()
    seen_blobs: set[str] = set()
    findings: list[tuple[str, str, int, str]] = []
    for commit in commits:
        entries = git(root, "ls-tree", "-r", "-z", commit).split(b"\0")
        for entry in entries:
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            blob = metadata.split()[2].decode("ascii")
            path = raw_path.decode("utf-8", errors="replace")
            if path_is_forbidden(path):
                findings.append(("forbidden_history_path", path, 0, commit[:12]))
                continue
            if blob in seen_blobs:
                continue
            seen_blobs.add(blob)
            findings.extend(
                scan_text(path, git(root, "cat-file", "blob", blob), commit[:12])
            )
    return findings


def dist_findings(root: Path) -> list[tuple[str, str, int, str]]:
    dist = root / "dist"
    if not dist.exists():
        return []
    allowed = {
        "proxy_core.py",
        "requirements.txt",
        "server.py",
        "version.json",
        "web/index.html",
        "web/update.svg",
        "web/locales/en.json",
        "web/locales/zh-CN.json",
    }
    findings: list[tuple[str, str, int, str]] = []
    for path in sorted(item for item in dist.rglob("*") if item.is_file()):
        relative = path.relative_to(dist).as_posix()
        if relative not in allowed:
            findings.append(("unexpected_dist_file", f"dist/{relative}", 0, "dist"))
            continue
        findings.extend(scan_text(f"dist/{relative}", path.read_bytes(), "dist"))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed when public release inputs contain private data."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    findings = sorted(set(tracked_findings(root) + history_findings(root) + dist_findings(root)))
    if findings:
        print("Privacy scan failed. Matched values are intentionally hidden.", file=sys.stderr)
        for category, path, line_number, ref in findings:
            location = f"{path}:{line_number}" if line_number else path
            print(f"- {category}: {location} ({ref})", file=sys.stderr)
        return 1
    print("Privacy scan passed: tracked files, Git history, and dist contain no detected private data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
