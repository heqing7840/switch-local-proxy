from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 9:
        raise SystemExit("usage: render_watchdog_launch_agent.py OUTPUT LABEL PYTHON SCRIPT SERVICE_LABEL SERVICE_PLIST PORT STATE_DIR")
    output, label, python, script, service_label, service_plist, port, state_dir = sys.argv[1:]
    payload = {
        "Label": label,
        "ProgramArguments": [
            python,
            script,
            service_label,
            service_plist,
            port,
        ],
        "RunAtLoad": True,
        "StartInterval": 15,
        "ProcessType": "Background",
        "StandardOutPath": str(Path(state_dir) / "watchdog.stdout.log"),
        "StandardErrorPath": str(Path(state_dir) / "watchdog.stderr.log"),
    }
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    os.chmod(destination, 0o600)


if __name__ == "__main__":
    main()
