from __future__ import annotations

import subprocess
import sys
import time
import urllib.request


def healthy(port: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as response:
            return response.status == 200 and b'"service":"Switch Local Proxy"' in response.read(4096).replace(b" ", b"")
    except (OSError, ValueError):
        return False


def main() -> int:
    if len(sys.argv) != 4:
        return 64
    label, plist, port = sys.argv[1:]
    if healthy(port):
        return 0
    time.sleep(3)
    if healthy(port):
        return 0
    domain = f"gui/{subprocess.check_output(['/usr/bin/id', '-u'], text=True).strip()}"
    subprocess.run(['/bin/launchctl', 'enable', f'{domain}/{label}'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(['/bin/launchctl', 'print', f'{domain}/{label}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        subprocess.run(['/bin/launchctl', 'bootstrap', domain, plist], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['/bin/launchctl', 'kickstart', '-k', f'{domain}/{label}'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
