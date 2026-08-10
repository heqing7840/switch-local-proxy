#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DIST="$ROOT/dist"
STATE_DIR="$ROOT/runtime"
LABEL="com.switch-local-proxy"
LEGACY_LABEL="cn.codex-key-proxy"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LEGACY_PLIST="$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
PYTHON="${SWITCH_LOCAL_PROXY_PYTHON:-${CODEX_KEY_PROXY_PYTHON:-$ROOT/.venv/bin/python3}}"
if [[ ! -x "$PYTHON" && -x "/opt/homebrew/bin/python3" ]]; then
  PYTHON="/opt/homebrew/bin/python3"
fi
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi
PORT="15722"

usage() {
  echo "Usage: ./run.sh build|verify|doctor|repair|import-keys SOURCE|install|migrate|start|stop|status|open"
}

health_check() {
  curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" | "$PYTHON" -c '
import json, sys
data = json.load(sys.stdin)
assert data.get("service") == "Switch Local Proxy"
assert data.get("ok") is True
assert any(item.get("has_key") for item in data.get("providers", []))
'
}

wait_for_health() {
  for _ in {1..40}; do
    if health_check >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

doctor() {
  [[ -n "$PYTHON" && -x "$PYTHON" ]] || { echo "FAIL: python3 不可用" >&2; return 1; }
  command -v curl >/dev/null || { echo "FAIL: curl 不可用" >&2; return 1; }
  command -v rg >/dev/null || { echo "FAIL: rg 不可用" >&2; return 1; }
  [[ -f "$ROOT/key.txt" ]] || { echo "FAIL: 项目 key.txt 不存在" >&2; return 1; }
  "$PYTHON" - "$ROOT" "$STATE_DIR" <<'PY'
import json
import os
import sys
from pathlib import Path

root, state_dir = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / "src"))
from proxy_core import parse_key_text

key_file = root / "key.txt"
keys = parse_key_text(key_file.read_text(encoding="utf-8"))
assert keys, "key.txt 中没有有效渠道"
assert os.stat(key_file).st_mode & 0o777 == 0o600, "key.txt 权限必须为 600"
for filename in ("settings.json", "runtime.json"):
    path = state_dir / filename
    if path.exists():
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), f"{filename} 格式无效"
print(f"配置检查通过：{len(keys)} 个渠道，未输出密钥")
PY
  plutil -lint "$PLIST" >/dev/null
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || {
    echo "FAIL: LaunchAgent 未加载，可运行 ./run.sh repair" >&2
    return 1
  }
  health_check >/dev/null || {
    echo "FAIL: 服务健康检查失败，可运行 ./run.sh repair" >&2
    return 1
  }
  echo "运行检查通过：LaunchAgent、端口、服务身份和可用密钥均正常"
}

build() {
  mkdir -p "$DIST/web" "$DIST/support"
  cp "$ROOT/src/proxy_core.py" "$DIST/proxy_core.py"
  cp "$ROOT/src/server.py" "$DIST/server.py"
  cp "$ROOT/src/web/index.html" "$DIST/web/index.html"
  mkdir -p "$DIST/web/locales"
  cp "$ROOT/src/web/locales/"*.json "$DIST/web/locales/"
  cp "$ROOT/requirements.txt" "$DIST/requirements.txt"
  chmod 600 "$DIST/proxy_core.py" "$DIST/server.py"
  echo "Built: $DIST"
}

verify() {
  "$PYTHON" -m unittest discover -s "$ROOT/tests" -v
  "$PYTHON" - "$ROOT/src/proxy_core.py" "$ROOT/src/server.py" <<'PY'
import ast
import pathlib
import sys
for filename in sys.argv[1:]:
    ast.parse(pathlib.Path(filename).read_text(encoding="utf-8"), filename=filename)
PY
  build
  "$PYTHON" - "$DIST/proxy_core.py" "$DIST/server.py" <<'PY'
import ast
import pathlib
import sys
for filename in sys.argv[1:]:
    ast.parse(pathlib.Path(filename).read_text(encoding="utf-8"), filename=filename)
PY
  "$PYTHON" - "$ROOT/src/web/locales/zh-CN.json" "$ROOT/src/web/locales/en.json" <<'PY'
import json
import re
import sys
from pathlib import Path
zh = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
en = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
assert set(zh) == set(en), "locale key sets differ"
token = re.compile(r"\{[^{}]+\}")
for key in zh:
    assert token.findall(zh[key]) == token.findall(en[key]), f"placeholder mismatch: {key}"
assert en["brandName"] == "Switch Local Proxy"
assert en["language.simplifiedChinese"] == "简体中文"
print(f"Locales verified: {len(en)} keys")
PY
  if rg -n 'sk-[A-Za-z0-9_-]{10,}|OPENAI_API_KEY[[:space:]]*=' "$DIST"; then
    echo "Secret-like content found in dist" >&2
    exit 1
  fi
  if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git -C "$ROOT" grep -n -E 'sk-[A-Za-z0-9_-]{30,}|/Users/|harin\.tokenha|dashscope\.aliyuncs' -- . ':!run.sh'; then
      echo "Private or secret-like content found in tracked files" >&2
      exit 1
    fi
  fi
  echo "Verification passed"
}

install_service() {
  mkdir -p "$STATE_DIR/backups"
  local backup_dir="$STATE_DIR/backups/install-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup_dir"
  [[ ! -d "$DIST" ]] || cp -R "$DIST" "$backup_dir/dist"
  [[ ! -f "$PLIST" ]] || cp "$PLIST" "$backup_dir/launch-agent.plist"
  verify
  mkdir -p "$STATE_DIR" "$HOME/Library/LaunchAgents"
  chmod 700 "$STATE_DIR"
  "$PYTHON" "$ROOT/support/render_launch_agent.py" \
    "$STATE_DIR/$LABEL.plist" "$LABEL" "$PYTHON" "$DIST/server.py" "$DIST" "$ROOT/key.txt" "$STATE_DIR" "$PORT"
  cp "$STATE_DIR/$LABEL.plist" "$PLIST"
  plutil -lint "$PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$LEGACY_LABEL" 2>/dev/null || true
  for _ in {1..20}; do
    if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
  if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || {
      echo "LaunchAgent bootstrap failed" >&2
      exit 1
    }
  fi
  launchctl enable "gui/$(id -u)/$LABEL"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  if ! wait_for_health; then
    echo "新版本健康检查失败，正在恢复上一版本" >&2
    if [[ -d "$backup_dir/dist" && -f "$backup_dir/launch-agent.plist" ]]; then
      cp -R "$backup_dir/dist/." "$DIST/"
      cp "$backup_dir/launch-agent.plist" "$PLIST"
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$PLIST"
      launchctl enable "gui/$(id -u)/$LABEL"
      launchctl kickstart -k "gui/$(id -u)/$LABEL"
      wait_for_health || true
    fi
    return 1
  fi
  echo "Installed: $PLIST"
  rm -f "$LEGACY_PLIST"
}

migrate_codex() {
  install_service
  mkdir -p "$ROOT/runtime/backups"
  cp "$HOME/.codex/config.toml" "$ROOT/runtime/backups/config.toml.$(date +%Y%m%d-%H%M%S)"
  "$PYTHON" "$ROOT/support/migrate_codex.py" "$HOME/.codex/config.toml" "http://127.0.0.1:$PORT/v1"
  echo "Codex now uses the local proxy on port $PORT"
}

start_service() {
  if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    [[ -f "$PLIST" ]] || { echo "LaunchAgent 未安装，请运行 ./run.sh install" >&2; return 1; }
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
  fi
  launchctl enable "gui/$(id -u)/$LABEL"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  wait_for_health || { echo "服务启动后健康检查失败，请运行 ./run.sh doctor" >&2; return 1; }
}

stop_service() {
  launchctl kill SIGTERM "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl disable "gui/$(id -u)/$LABEL"
  launchctl kill SIGTERM "gui/$(id -u)/$LEGACY_LABEL" 2>/dev/null || true
  launchctl disable "gui/$(id -u)/$LEGACY_LABEL" 2>/dev/null || true
}

status_service() {
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | rg 'state =|pid =|runs =|last exit code' || true
  curl -fsS "http://127.0.0.1:$PORT/api/health" | "$PYTHON" -m json.tool
}

import_keys() {
  local source="${2:-}"
  [[ -n "$source" ]] || { echo "Usage: ./run.sh import-keys /path/to/source/key.txt" >&2; return 1; }
  "$PYTHON" "$ROOT/support/import_keys.py" \
    "$source" \
    "$ROOT/key.txt"
}

case "${1:-}" in
  build) build ;;
  verify) verify ;;
  doctor) doctor ;;
  repair) install_service ;;
  import-keys) import_keys "$@" ;;
  install) install_service ;;
  migrate) migrate_codex ;;
  start) start_service ;;
  stop) stop_service ;;
  status) status_service ;;
  open) open "http://127.0.0.1:$PORT/" ;;
  *) usage; exit 1 ;;
esac
