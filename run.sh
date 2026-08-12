#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DIST="$ROOT/dist"
STATE_DIR="$ROOT/runtime"
LABEL="com.switch-local-proxy"
WATCHDOG_LABEL="$LABEL.watchdog"
LEGACY_LABEL="cn.codex-key-proxy"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WATCHDOG_PLIST="$HOME/Library/LaunchAgents/$WATCHDOG_LABEL.plist"
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
  echo "Usage: ./run.sh build|privacy-check|verify|doctor|repair|install|migrate|start|stop|status|open"
}

# 仅确认进程已起来且身份正确；首次安装尚无渠道时也算成功
service_alive_check() {
  curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" | "$PYTHON" -c '
import json, sys
data = json.load(sys.stdin)
assert data.get("service") == "Switch Local Proxy"
'
}

# 完整就绪：服务正常且至少有一条可用密钥
providers_ready_check() {
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
    if service_alive_check >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

load_launch_agent() {
  local domain="gui/$(id -u)"
  if launchctl bootstrap "$domain" "$PLIST" 2>/dev/null; then
    return 0
  fi
  # Some macOS login sessions reject bootstrap with EIO even though the
  # same per-user plist remains compatible with the legacy load command.
  # Confirm the service state first so a harmless duplicate-load error is
  # not treated as an installation failure.
  if launchctl print "$domain/$LABEL" >/dev/null 2>&1; then
    return 0
  fi
  launchctl load -w "$PLIST"
}

load_watchdog_agent() {
  local domain="gui/$(id -u)"
  if launchctl bootstrap "$domain" "$WATCHDOG_PLIST" 2>/dev/null; then
    return 0
  fi
  if launchctl print "$domain/$WATCHDOG_LABEL" >/dev/null 2>&1; then
    return 0
  fi
  launchctl load -w "$WATCHDOG_PLIST"
}

# 优先用 rg；普通终端没有 ripgrep 时回退到 grep -E，避免迁移后误报依赖失败
match_text() {
  local pattern="$1"
  if command -v rg >/dev/null 2>&1; then
    rg -q -- "$pattern"
  else
    grep -Eq -- "$pattern"
  fi
}

filter_text() {
  local pattern="$1"
  if command -v rg >/dev/null 2>&1; then
    rg -- "$pattern" || true
  else
    grep -E -- "$pattern" || true
  fi
}

doctor() {
  [[ -n "$PYTHON" && -x "$PYTHON" ]] || { echo "FAIL: python3 不可用" >&2; return 1; }
  command -v curl >/dev/null || { echo "FAIL: curl 不可用" >&2; return 1; }
  local provider_count=0
  provider_count="$("$PYTHON" - "$ROOT" "$STATE_DIR" <<'PY'
import sqlite3
import sys
from pathlib import Path

root, state_dir = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / "src"))
from proxy_core import ProxyStore

database = state_dir / "proxy.sqlite3"
assert database.is_file(), "本机 SQLite 数据库不存在"
with sqlite3.connect(database) as connection:
    connection.execute("SELECT 1 FROM proxy_data LIMIT 1").fetchone()
store = ProxyStore(state_dir)
keys = store.load_keys()
print(len(keys))
PY
)" || {
    echo "FAIL: 本机配置数据库不可用，请运行 ./run.sh repair" >&2
    return 1
  }
  if [[ "$provider_count" -gt 0 ]]; then
    echo "配置检查通过：${provider_count} 个渠道，未输出密钥"
  else
    echo "WARN: 尚未配置渠道密钥，请打开管理页添加后再转发请求"
  fi
  plutil -lint "$PLIST" >/dev/null
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :RunAtLoad' "$PLIST" 2>/dev/null)" == "true" ]] || {
    echo "FAIL: LaunchAgent 未启用登录自动启动，请运行 ./run.sh repair" >&2
    return 1
  }
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :KeepAlive' "$PLIST" 2>/dev/null)" == "true" ]] || {
    echo "FAIL: LaunchAgent 未启用异常退出自动重启，请运行 ./run.sh repair" >&2
    return 1
  }
  plutil -lint "$WATCHDOG_PLIST" >/dev/null || {
    echo "FAIL: 健康守护 LaunchAgent 配置无效，请运行 ./run.sh repair" >&2
    return 1
  }
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :RunAtLoad' "$WATCHDOG_PLIST" 2>/dev/null)" == "true" ]] || {
    echo "FAIL: 健康守护未启用登录自动启动，请运行 ./run.sh repair" >&2
    return 1
  }
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :StartInterval' "$WATCHDOG_PLIST" 2>/dev/null)" == "15" ]] || {
    echo "FAIL: 健康守护检查间隔不是 15 秒，请运行 ./run.sh repair" >&2
    return 1
  }
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || {
    echo "FAIL: LaunchAgent 未加载，可运行 ./run.sh repair" >&2
    return 1
  }
  launchctl print "gui/$(id -u)/$WATCHDOG_LABEL" >/dev/null 2>&1 || {
    echo "FAIL: 健康守护 LaunchAgent 未加载，可运行 ./run.sh repair" >&2
    return 1
  }
  local watchdog_state
  watchdog_state="$(launchctl print "gui/$(id -u)/$WATCHDOG_LABEL" 2>/dev/null)"
  if print -r -- "$watchdog_state" | match_text 'last exit code = [1-9][0-9]*'; then
    echo "FAIL: 健康守护最近一次运行失败，请运行 ./run.sh repair" >&2
    return 1
  fi
  # 安装后允许暂无密钥；只要服务身份正确即可
  service_alive_check >/dev/null || {
    echo "FAIL: 服务健康检查失败，可运行 ./run.sh repair" >&2
    return 1
  }
  if providers_ready_check >/dev/null 2>&1; then
    echo "运行检查通过：登录自启、异常重启、15 秒健康守护、端口、服务身份和可用密钥均正常"
  else
    echo "运行检查通过：登录自启、异常重启、15 秒健康守护、端口和服务身份均正常"
    echo "下一步：执行 ./run.sh open，在管理页添加至少一条渠道密钥"
  fi
}

build() {
  "$PYTHON" - "$DIST" <<'PY'
import shutil
import sys
from pathlib import Path
dist = Path(sys.argv[1])
if dist.exists():
    shutil.rmtree(dist)
PY
  mkdir -p "$DIST/web" "$DIST/support"
  cp "$ROOT/src/proxy_core.py" "$DIST/proxy_core.py"
  cp "$ROOT/src/server.py" "$DIST/server.py"
  cp "$ROOT/src/web/index.html" "$DIST/web/index.html"
  cp "$ROOT/src/web/update.svg" "$DIST/web/update.svg"
  cp "$ROOT/version.json" "$DIST/version.json"
  mkdir -p "$DIST/web/locales"
  cp "$ROOT/src/web/locales/"*.json "$DIST/web/locales/"
  cp "$ROOT/requirements.txt" "$DIST/requirements.txt"
  cp "$ROOT/support/watchdog.py" "$DIST/support/watchdog.py"
  chmod 600 "$DIST/support/watchdog.py"
  chmod 600 "$DIST/proxy_core.py" "$DIST/server.py"
  echo "Built: $DIST"
}

clean_dist_bytecode() {
  "$PYTHON" - "$DIST" <<'PY'
import shutil
import sys
from pathlib import Path
for path in Path(sys.argv[1]).rglob("__pycache__"):
    shutil.rmtree(path)
PY
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
  "$PYTHON" - "$ROOT/support/render_watchdog_launch_agent.py" <<'PY'
import ast
import pathlib
import sys
filename = sys.argv[1]
ast.parse(pathlib.Path(filename).read_text(encoding="utf-8"), filename=filename)
PY
  build
  clean_dist_bytecode
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
  "$PYTHON" "$ROOT/support/privacy_scan.py" --root "$ROOT"
  echo "Verification passed"
}

install_service() {
  mkdir -p "$STATE_DIR/backups"
  local backup_dir="$STATE_DIR/backups/install-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup_dir"
  [[ ! -d "$DIST" ]] || cp -R "$DIST" "$backup_dir/dist"
  [[ ! -f "$PLIST" ]] || cp "$PLIST" "$backup_dir/launch-agent.plist"
  [[ ! -f "$WATCHDOG_PLIST" ]] || cp "$WATCHDOG_PLIST" "$backup_dir/watchdog-launch-agent.plist"
  verify
  mkdir -p "$STATE_DIR" "$HOME/Library/LaunchAgents"
  chmod 700 "$STATE_DIR"
  "$PYTHON" "$ROOT/support/render_launch_agent.py" \
    "$STATE_DIR/$LABEL.plist" "$LABEL" "$PYTHON" "$DIST/server.py" "$DIST" "$STATE_DIR" "$PORT"
  "$PYTHON" "$ROOT/support/render_watchdog_launch_agent.py" \
    "$STATE_DIR/$WATCHDOG_LABEL.plist" "$WATCHDOG_LABEL" "$PYTHON" "$DIST/support/watchdog.py" \
    "$LABEL" "$PLIST" "$PORT" "$STATE_DIR"
  cp "$STATE_DIR/$LABEL.plist" "$PLIST"
  cp "$STATE_DIR/$WATCHDOG_LABEL.plist" "$WATCHDOG_PLIST"
  plutil -lint "$PLIST"
  plutil -lint "$WATCHDOG_PLIST"
  launchctl bootout "gui/$(id -u)/$WATCHDOG_LABEL" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$LEGACY_LABEL" 2>/dev/null || true
  for _ in {1..20}; do
    if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
  load_launch_agent || {
    echo "LaunchAgent load failed" >&2
    exit 1
  }
  launchctl enable "gui/$(id -u)/$LABEL"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  load_watchdog_agent
  launchctl enable "gui/$(id -u)/$WATCHDOG_LABEL"
  launchctl kickstart "gui/$(id -u)/$WATCHDOG_LABEL" 2>/dev/null || true
  if ! wait_for_health; then
    echo "新版本健康检查失败，正在恢复上一版本" >&2
    if [[ -d "$backup_dir/dist" && -f "$backup_dir/launch-agent.plist" ]]; then
      cp -R "$backup_dir/dist/." "$DIST/"
      cp "$backup_dir/launch-agent.plist" "$PLIST"
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      load_launch_agent
      launchctl enable "gui/$(id -u)/$LABEL"
      launchctl kickstart -k "gui/$(id -u)/$LABEL"
      wait_for_health || true
    fi
    return 1
  fi
  clean_dist_bytecode
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
    load_launch_agent
  fi
  launchctl enable "gui/$(id -u)/$LABEL"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  if ! launchctl print "gui/$(id -u)/$WATCHDOG_LABEL" >/dev/null 2>&1; then
    [[ -f "$WATCHDOG_PLIST" ]] || { echo "健康守护未安装，请运行 ./run.sh install" >&2; return 1; }
    load_watchdog_agent
  fi
  launchctl enable "gui/$(id -u)/$WATCHDOG_LABEL"
  launchctl kickstart "gui/$(id -u)/$WATCHDOG_LABEL" 2>/dev/null || true
  wait_for_health || { echo "服务启动后健康检查失败，请运行 ./run.sh doctor" >&2; return 1; }
}

stop_service() {
  launchctl bootout "gui/$(id -u)/$WATCHDOG_LABEL" 2>/dev/null || true
  launchctl disable "gui/$(id -u)/$WATCHDOG_LABEL" 2>/dev/null || true
  launchctl kill SIGTERM "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl disable "gui/$(id -u)/$LABEL"
  launchctl kill SIGTERM "gui/$(id -u)/$LEGACY_LABEL" 2>/dev/null || true
  launchctl disable "gui/$(id -u)/$LEGACY_LABEL" 2>/dev/null || true
}

status_service() {
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | filter_text 'state =|pid =|runs =|last exit code'
  echo "Watchdog:"
  launchctl print "gui/$(id -u)/$WATCHDOG_LABEL" 2>/dev/null | filter_text 'state =|runs =|last exit code'
  curl -fsS "http://127.0.0.1:$PORT/api/health" | "$PYTHON" -m json.tool
}

case "${1:-}" in
  build) build ;;
  privacy-check) "$PYTHON" "$ROOT/support/privacy_scan.py" --root "$ROOT" ;;
  verify) verify ;;
  doctor) doctor ;;
  repair) install_service ;;
  install) install_service ;;
  migrate) migrate_codex ;;
  start) start_service ;;
  stop) stop_service ;;
  status) status_service ;;
  open) open "http://127.0.0.1:$PORT/" ;;
  *) usage; exit 1 ;;
esac
