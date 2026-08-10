from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 9:
        raise SystemExit(
            "usage: render_launch_agent.py OUTPUT LABEL PYTHON SERVER WORKDIR KEY_FILE STATE_DIR PORT"
        )
    output, label, python, server, workdir, key_file, state_dir, port = sys.argv[1:]
    payload = {
        "Label": label,
        "ProgramArguments": [python, server],
        "WorkingDirectory": workdir,
        "EnvironmentVariables": {
            "SWITCH_LOCAL_PROXY_HOST": "127.0.0.1",
            "SWITCH_LOCAL_PROXY_PORT": port,
            "SWITCH_LOCAL_PROXY_KEY_FILE": key_file,
            "SWITCH_LOCAL_PROXY_STATE_DIR": state_dir,
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(Path(state_dir) / "service.stdout.log"),
        "StandardErrorPath": str(Path(state_dir) / "service.stderr.log"),
    }
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    os.chmod(destination, 0o600)


if __name__ == "__main__":
    main()
