import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "support" / "watchdog.py"
spec = importlib.util.spec_from_file_location("switch_local_proxy_watchdog", path)
watchdog = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(watchdog)


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return json.dumps({"service": "Switch Local Proxy"}).encode()


class WatchdogTests(unittest.TestCase):
    def test_health_parses_service_name_with_spaces(self):
        with patch.object(watchdog.urllib.request, "urlopen", return_value=Response()):
            self.assertTrue(watchdog.healthy("15722"))

    def test_live_listener_is_not_force_restarted(self):
        with patch.object(watchdog, "healthy", return_value=False), patch.object(
            watchdog, "accepting_connections", return_value=True
        ), patch.object(watchdog.time, "sleep"), patch.object(
            watchdog.subprocess, "run"
        ) as run, patch.object(
            sys, "argv", ["watchdog.py", "label", "plist", "15722"]
        ):
            self.assertEqual(watchdog.main(), 0)
            run.assert_not_called()
