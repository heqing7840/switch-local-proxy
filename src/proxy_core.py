from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_PROVIDER_NAMES = [
    "provider-1",
    "provider-2",
    "provider-3",
    "provider-4",
    "provider-5",
]

RETRYABLE_STATUS_CODES = {401, 403, 408, 409, 425, 429}
KEY_LINE_RE = re.compile(r"^\s*([^:#][^:]*?)\s*:\s*(sk-[^\s]+)\s*$")
PROVIDER_NAME_RE = re.compile(r"^[^:\r\n\x00-\x1f\x7f]{1,80}$")
PROVIDER_KEY_RE = re.compile(r"^sk-[^\s]{8,500}$")
MAX_PROVIDERS = 30
SAME_PROVIDER_502_RETRIES = 1
CONTEXT_ESTIMATOR_VERSION = 2
PRIVATE_SETTING_NAMES = {"upstream_url", "forced_model"}
OPENAI_RESPONSES_ADAPTER = "openai_responses"
ANTHROPIC_MESSAGES_ADAPTER = "anthropic_messages"
SUPPORTED_ADAPTERS = (OPENAI_RESPONSES_ADAPTER, ANTHROPIC_MESSAGES_ADAPTER)


def adapter_for_path(path: str) -> str | None:
    if path == "/v1/responses":
        return OPENAI_RESPONSES_ADAPTER
    if path in {"/v1/messages", "/v1/messages/count_tokens"}:
        return ANTHROPIC_MESSAGES_ADAPTER
    return None


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def parse_key_text(text: str) -> dict[str, str]:
    providers: dict[str, str] = {}
    for line in text.splitlines():
        match = KEY_LINE_RE.match(line)
        if match:
            providers[match.group(1).strip()] = match.group(2).strip()
    return providers


def parse_private_settings(text: str) -> dict[str, str]:
    settings: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if separator and name in PRIVATE_SETTING_NAMES and value:
            settings[name] = value
    return settings


def valid_upstream_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_provider_name(value: str) -> str:
    name = value.strip()
    if not PROVIDER_NAME_RE.fullmatch(name):
        raise ValueError("渠道名称需为 1-80 个字符，且不能包含冒号或换行")
    return name


def validate_provider_key(value: str) -> str:
    key = value.strip()
    if not PROVIDER_KEY_RE.fullmatch(key):
        raise ValueError("密钥格式无效")
    return key


def mask_provider_key(value: str) -> str:
    if not value:
        return ""
    return f"{value[:6]}…{value[-4:]}"


def estimate_input_tokens(payload: bytes) -> int:
    """Estimate text input size while excluding large inline binary payloads."""
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = payload.decode("utf-8", errors="replace")

    parts: list[str] = []

    def collect(value: Any, field_name: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, str(key))
        elif isinstance(value, list):
            for item in value:
                collect(item, field_name)
        elif isinstance(value, str):
            lowered = field_name.lower()
            if lowered in {"file_data", "image_url"} and (
                value.startswith("data:") or len(value) > 100_000
            ):
                parts.append("[binary attachment]")
            else:
                parts.append(value)

    collect(decoded)
    text = "\n".join(parts)
    non_ascii = sum(1 for char in text if ord(char) > 127)
    ascii_count = len(text) - non_ascii
    return max(1, round(non_ascii + ascii_count / 4))


def should_retry_same_provider(status_code: int, retry_count: int) -> bool:
    return status_code == 502 and retry_count < SAME_PROVIDER_502_RETRIES


def is_context_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "context window",
            "context length",
            "input exceeds",
            "上下文",
            "超出上下文",
        )
    )


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES or status_code >= 500


def inspect_responses_payload(payload: bytes) -> str | None:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(decoded, dict):
        return None
    error = decoded.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "Responses error")
    if decoded.get("status") == "failed":
        nested = decoded.get("response")
        if isinstance(nested, dict):
            nested_error = nested.get("error")
            if isinstance(nested_error, dict):
                return str(nested_error.get("message") or "Response failed")
        return "Response failed"
    return None


def inspect_sse_prime(payload: bytes) -> tuple[str, str | None]:
    text = payload.decode("utf-8", errors="replace")
    lowered = text.lower()
    if (
        "event: error" in lowered
        or "event: response.failed" in lowered
        or '"type":"error"' in lowered.replace(" ", "")
        or '"type":"response.failed"' in lowered.replace(" ", "")
        or '"status":"failed"' in lowered.replace(" ", "")
    ):
        message = "Responses SSE failure"
        for raw_line in text.splitlines():
            if not raw_line.startswith("data:"):
                continue
            candidate = raw_line[5:].strip()
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                error = decoded.get("error")
                response = decoded.get("response")
                if isinstance(error, dict):
                    message = str(error.get("message") or error.get("code") or message)
                    break
                if isinstance(response, dict) and isinstance(response.get("error"), dict):
                    message = str(response["error"].get("message") or message)
                    break
        return "error", message

    productive_markers = (
        "event: response.output_text.delta",
        "event: response.function_call_arguments.delta",
        "event: response.completed",
        "event: content_block_delta",
        "event: message_delta",
        "event: message_stop",
        '"type":"response.output_text.delta"',
        '"type":"response.function_call_arguments.delta"',
        '"type":"response.completed"',
        '"type":"content_block_delta"',
        '"type":"message_delta"',
        '"type":"message_stop"',
    )
    compact = lowered.replace(" ", "")
    if any(marker in lowered or marker in compact for marker in productive_markers):
        return "productive", None
    return "pending", None


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.chmod(temp_name, 0o600)
    os.replace(temp_name, path)


class ProxyStore:
    def __init__(self, state_dir: Path, key_file: Path):
        self.state_dir = state_dir
        self.key_file = key_file
        self.settings_path = state_dir / "settings.json"
        self.runtime_path = state_dir / "runtime.json"
        self.events_path = state_dir / "events.jsonl"
        self.lock = threading.RLock()
        self.started_at = time.time()
        self.last_switch: dict[str, Any] | None = None
        self.active_provider: str | None = None
        self.settings = self._load_settings()
        self.runtime = self._load_runtime()
        self.events = self._load_events()

    def _default_settings(self) -> dict[str, Any]:
        try:
            key_text = self.key_file.read_text(encoding="utf-8")
            names = list(parse_key_text(key_text))
            private = parse_private_settings(key_text)
        except OSError:
            names = list(DEFAULT_PROVIDER_NAMES)
            private = {}
        upstream_url = (
            os.environ.get("SWITCH_LOCAL_PROXY_UPSTREAM_URL")
            or os.environ.get("CODEX_KEY_PROXY_UPSTREAM_URL")
            or private.get("upstream_url", "")
        ).rstrip("/")
        forced_model = (
            os.environ.get("SWITCH_LOCAL_PROXY_FORCED_MODEL")
            or os.environ.get("CODEX_KEY_PROXY_FORCED_MODEL")
            or private.get("forced_model", "")
        )
        return {
            "cooldown_seconds": 300,
            "forced_model": forced_model,
            "upstream_base_url": upstream_url,
            "providers": [
                {"name": name, "enabled": True, "priority": index + 1}
                for index, name in enumerate(names)
            ],
        }

    def _load_settings(self) -> dict[str, Any]:
        default = self._default_settings()
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        if not default["upstream_base_url"]:
            saved_url = str(saved.get("upstream_base_url") or "").rstrip("/")
            if valid_upstream_url(saved_url):
                default["upstream_base_url"] = saved_url
        if not default["forced_model"]:
            default["forced_model"] = str(saved.get("forced_model") or "")[:100]
        default["cooldown_seconds"] = max(
            60, min(3600, safe_int(saved.get("cooldown_seconds"), 300))
        )
        saved_providers = {
            item.get("name"): item
            for item in saved.get("providers", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        try:
            key_names = list(self.load_keys())
        except OSError:
            key_names = []
        ordered_names = [item["name"] for item in default["providers"]]
        for item in sorted(
            saved_providers.values(),
            key=lambda value: safe_int(value.get("priority"), 999),
        ):
            if item["name"] not in ordered_names:
                ordered_names.append(item["name"])
        for name in key_names:
            if name not in ordered_names:
                ordered_names.append(name)
        providers = []
        for index, name in enumerate(ordered_names):
            item = saved_providers.get(name, {})
            providers.append(
                {
                    "name": name,
                    "enabled": bool(item.get("enabled", True)),
                    "priority": safe_int(item.get("priority"), index + 1),
                }
            )
        default["providers"] = self._normalize_priorities(providers)
        atomic_write_json(self.settings_path, default)
        return default

    def _load_runtime(self) -> dict[str, dict[str, Any]]:
        try:
            saved = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        runtime: dict[str, dict[str, Any]] = {}
        for provider in self.settings["providers"]:
            name = provider["name"]
            item = saved.get(name, {})
            runtime[name] = self._runtime_state(item)
        return runtime

    @staticmethod
    def _runtime_state(item: dict[str, Any] | None = None) -> dict[str, Any]:
        item = item or {}
        estimator_is_current = (
            safe_int(item.get("context_estimator_version"), 0) == CONTEXT_ESTIMATOR_VERSION
        )
        return {
            "cooldown_until": safe_float(item.get("cooldown_until"), 0),
            "last_error": str(item.get("last_error", ""))[:300],
            "last_success_at": item.get("last_success_at"),
            "last_failure_at": item.get("last_failure_at"),
            "last_latency_ms": item.get("last_latency_ms"),
            "success_count": max(0, safe_int(item.get("success_count"), 0)),
            "failure_count": max(0, safe_int(item.get("failure_count"), 0)),
            "max_success_input_tokens": max(
                0, safe_int(item.get("max_success_input_tokens"), 0)
            ) if estimator_is_current else 0,
            "min_context_failure_tokens": max(
                0, safe_int(item.get("min_context_failure_tokens"), 0)
            ) if estimator_is_current else 0,
            "context_success_count": max(
                0, safe_int(item.get("context_success_count"), 0)
            ) if estimator_is_current else 0,
            "context_failure_count": max(
                0, safe_int(item.get("context_failure_count"), 0)
            ) if estimator_is_current else 0,
            "context_estimator_version": CONTEXT_ESTIMATOR_VERSION,
            "probing": False,
        }

    def _load_events(self) -> list[dict[str, Any]]:
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()[-200:]
        except OSError:
            return []
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _append_event(
        self,
        name: str,
        result: str,
        latency_ms: int | None,
        message: str,
        requested_model: str | None = None,
        upstream_model: str | None = None,
        request_id: str | None = None,
        input_tokens_estimate: int | None = None,
        retry_count: int = 0,
        adapter: str = OPENAI_RESPONSES_ADAPTER,
    ) -> None:
        event = {
            "id": f"{time.time_ns()}",
            "at": time.time(),
            "provider": name,
            "result": result,
            "latency_ms": latency_ms,
            "message": message[:300],
            "requested_model": str(requested_model or "")[:100],
            "model": str(upstream_model or "")[:100],
            "request_id": str(request_id or "")[:40],
            "input_tokens_estimate": input_tokens_estimate,
            "input_estimator_version": CONTEXT_ESTIMATOR_VERSION,
            "retry_count": max(0, retry_count),
            "adapter": adapter if adapter in SUPPORTED_ADAPTERS else OPENAI_RESPONSES_ADAPTER,
        }
        self.events.append(event)
        self.events = self.events[-200:]
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.chmod(self.events_path, 0o600)

    @staticmethod
    def _normalize_priorities(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(providers, key=lambda item: (item.get("priority", 999), item["name"]))
        for index, item in enumerate(ordered):
            item["priority"] = index + 1
        return ordered

    def load_keys(self) -> dict[str, str]:
        return parse_key_text(self.key_file.read_text(encoding="utf-8"))

    def _write_keys(self, keys: dict[str, str]) -> None:
        try:
            private = parse_private_settings(self.key_file.read_text(encoding="utf-8"))
        except OSError:
            private = {}
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.key_file.parent, delete=False
        ) as handle:
            for name in ("upstream_url", "forced_model"):
                if private.get(name):
                    handle.write(f"{name}:{private[name]}\n")
            for name, key in keys.items():
                handle.write(f"{name}:{key}\n")
            temp_name = handle.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.key_file)

    def create_provider(self, name: str, key: str) -> None:
        name = validate_provider_name(name)
        key = validate_provider_key(key)
        with self.lock:
            providers = self._normalize_priorities(list(self.settings["providers"]))
            if len(providers) >= MAX_PROVIDERS:
                raise ValueError(f"最多支持 {MAX_PROVIDERS} 个渠道")
            if any(item["name"] == name for item in providers):
                raise ValueError("渠道名称已存在")
            keys = self.load_keys() if self.key_file.exists() else {}
            keys[name] = key
            providers.append({"name": name, "enabled": True, "priority": len(providers) + 1})
            self._write_keys(keys)
            self.runtime[name] = self._runtime_state()
            self.settings["providers"] = self._normalize_priorities(providers)
            atomic_write_json(self.settings_path, self.settings)
            self._save_runtime()

    def update_provider(self, old_name: str, name: str, key: str | None = None) -> None:
        old_name = validate_provider_name(old_name)
        name = validate_provider_name(name)
        new_key = validate_provider_key(key) if key and key.strip() else None
        with self.lock:
            providers = self._normalize_priorities(list(self.settings["providers"]))
            index = next(
                (i for i, item in enumerate(providers) if item["name"] == old_name), None
            )
            if index is None:
                raise KeyError(old_name)
            if name != old_name and any(item["name"] == name for item in providers):
                raise ValueError("渠道名称已存在")
            keys = self.load_keys() if self.key_file.exists() else {}
            existing_key = keys.get(old_name)
            if new_key is None and existing_key is None:
                raise ValueError("该渠道缺少密钥，请输入新密钥")
            ordered_keys: dict[str, str] = {}
            for current_name, current_key in keys.items():
                if current_name == old_name:
                    ordered_keys[name] = new_key or current_key
                else:
                    ordered_keys[current_name] = current_key
            if old_name not in keys:
                ordered_keys[name] = new_key or ""
            providers[index]["name"] = name
            if name != old_name:
                self.runtime[name] = self.runtime.pop(old_name, self._runtime_state())
                if self.active_provider == old_name:
                    self.active_provider = name
                if self.last_switch and self.last_switch.get("provider") == old_name:
                    self.last_switch["provider"] = name
            self._write_keys(ordered_keys)
            self.settings["providers"] = providers
            atomic_write_json(self.settings_path, self.settings)
            self._save_runtime()

    def delete_provider(self, name: str) -> None:
        name = validate_provider_name(name)
        with self.lock:
            providers = self._normalize_priorities(list(self.settings["providers"]))
            if not any(item["name"] == name for item in providers):
                raise KeyError(name)
            keys = self.load_keys() if self.key_file.exists() else {}
            keys.pop(name, None)
            providers = [item for item in providers if item["name"] != name]
            self._write_keys(keys)
            self.runtime.pop(name, None)
            if self.active_provider == name:
                self.active_provider = None
            self.settings["providers"] = self._normalize_priorities(providers)
            atomic_write_json(self.settings_path, self.settings)
            self._save_runtime()

    def eligible_providers(self) -> list[dict[str, Any]]:
        keys = self.load_keys()
        now = time.time()
        with self.lock:
            result = []
            for provider in self._normalize_priorities(list(self.settings["providers"])):
                state = self.runtime[provider["name"]]
                if not provider["enabled"] or provider["name"] not in keys:
                    continue
                if state["cooldown_until"] > now or state["probing"]:
                    continue
                was_cooling = state["cooldown_until"] > 0
                if was_cooling:
                    state["probing"] = True
                result.append(
                    {
                        "name": provider["name"],
                        "key": keys[provider["name"]],
                        "priority": provider["priority"],
                        "was_cooling": was_cooling,
                    }
                )
            return result

    def set_active_provider(self, name: str | None) -> None:
        with self.lock:
            self.active_provider = name

    def release_untried_probe(self, provider: dict[str, Any]) -> None:
        if provider.get("was_cooling"):
            with self.lock:
                self.runtime[provider["name"]]["probing"] = False

    def mark_success(
        self,
        name: str,
        latency_ms: int,
        requested_model: str | None = None,
        upstream_model: str | None = None,
        request_id: str | None = None,
        input_tokens_estimate: int | None = None,
        retry_count: int = 0,
        adapter: str = OPENAI_RESPONSES_ADAPTER,
    ) -> None:
        now = time.time()
        with self.lock:
            state = self.runtime[name]
            state.update(
                {
                    "cooldown_until": 0,
                    "last_error": "",
                    "last_success_at": now,
                    "last_latency_ms": latency_ms,
                    "success_count": state["success_count"] + 1,
                    "probing": False,
                }
            )
            if input_tokens_estimate:
                state["max_success_input_tokens"] = max(
                    state["max_success_input_tokens"], input_tokens_estimate
                )
                state["context_success_count"] += 1
            self.active_provider = name
            self.last_switch = {"provider": name, "at": now}
            self._append_event(
                name,
                "success",
                latency_ms,
                "请求成功",
                requested_model,
                upstream_model,
                request_id,
                input_tokens_estimate,
                retry_count,
                adapter,
            )
            self._save_runtime()

    def mark_failure(
        self,
        name: str,
        error: str,
        latency_ms: int | None = None,
        requested_model: str | None = None,
        upstream_model: str | None = None,
        request_id: str | None = None,
        input_tokens_estimate: int | None = None,
        retry_count: int = 0,
        adapter: str = OPENAI_RESPONSES_ADAPTER,
    ) -> None:
        now = time.time()
        with self.lock:
            state = self.runtime[name]
            state.update(
                {
                    "cooldown_until": now + self.settings["cooldown_seconds"],
                    "last_error": error[:300],
                    "last_failure_at": now,
                    "failure_count": state["failure_count"] + 1,
                    "probing": False,
                }
            )
            if input_tokens_estimate and is_context_error(error):
                current = state["min_context_failure_tokens"]
                state["min_context_failure_tokens"] = (
                    input_tokens_estimate if not current else min(current, input_tokens_estimate)
                )
                state["context_failure_count"] += 1
            self._append_event(
                name,
                "failure",
                latency_ms,
                error,
                requested_model,
                upstream_model,
                request_id,
                input_tokens_estimate,
                retry_count,
                adapter,
            )
            self._save_runtime()

    def _save_runtime(self) -> None:
        serializable = {
            name: {key: value for key, value in state.items() if key != "probing"}
            for name, state in self.runtime.items()
        }
        atomic_write_json(self.runtime_path, serializable)

    def status(self) -> dict[str, Any]:
        now = time.time()
        try:
            keys = self.load_keys()
            key_error = None
        except OSError as error:
            keys = {}
            key_error = str(error)
        with self.lock:
            last_success_provider = None
            if self.runtime and any(
                item.get("last_success_at") for item in self.runtime.values()
            ):
                last_success_provider = max(
                    self.runtime.items(),
                    key=lambda item: item[1].get("last_success_at") or 0,
                )[0]
            current_provider = self.active_provider or last_success_provider
            providers = []
            for provider in self._normalize_priorities(list(self.settings["providers"])):
                state = self.runtime[provider["name"]]
                remaining = max(0, int(state["cooldown_until"] - now))
                if not provider["enabled"]:
                    health = "disabled"
                elif provider["name"] not in keys:
                    health = "missing"
                elif state["probing"]:
                    health = "probing"
                elif remaining > 0:
                    health = "cooling"
                else:
                    health = "ready"
                providers.append(
                    {
                        **provider,
                        "health": health,
                        "cooldown_remaining": remaining,
                        "last_error": state["last_error"],
                        "last_success_at": state["last_success_at"],
                        "last_failure_at": state["last_failure_at"],
                        "last_latency_ms": state["last_latency_ms"],
                        "success_count": state["success_count"],
                        "failure_count": state["failure_count"],
                        "has_key": provider["name"] in keys,
                        "key_hint": mask_provider_key(keys.get(provider["name"], "")),
                        "is_current": provider["name"] == current_provider,
                        "max_success_input_tokens": state["max_success_input_tokens"] or None,
                        "min_context_failure_tokens": state["min_context_failure_tokens"] or None,
                        "context_success_count": state["context_success_count"],
                        "context_failure_count": state["context_failure_count"],
                    }
                )
            configuration_error = None
            if not valid_upstream_url(str(self.settings.get("upstream_base_url") or "")):
                configuration_error = "upstream_url is missing or invalid"
            elif not str(self.settings.get("forced_model") or ""):
                configuration_error = "forced_model is missing"
            return {
                "ok": key_error is None and bool(keys) and configuration_error is None,
                "service": "Switch Local Proxy",
                "host": "127.0.0.1",
                "port": 15722,
                "uptime_seconds": int(now - self.started_at),
                "cooldown_seconds": self.settings["cooldown_seconds"],
                "forced_model": self.settings["forced_model"],
                "supported_adapters": list(SUPPORTED_ADAPTERS),
                "last_switch": self.last_switch,
                "current_provider": current_provider,
                "key_error": key_error,
                "configuration_error": configuration_error,
                "providers": providers,
                "events": list(reversed(self.events[-200:])),
            }

    def update_cooldown(self, seconds: int) -> None:
        with self.lock:
            self.settings["cooldown_seconds"] = max(60, min(3600, int(seconds)))
            atomic_write_json(self.settings_path, self.settings)

    def provider_action(self, name: str, action: str) -> None:
        with self.lock:
            providers = self._normalize_priorities(list(self.settings["providers"]))
            index = next((i for i, item in enumerate(providers) if item["name"] == name), None)
            if index is None:
                raise KeyError(name)
            if action == "toggle":
                providers[index]["enabled"] = not providers[index]["enabled"]
            elif action == "reset":
                self.runtime[name].update(
                    {"cooldown_until": 0, "last_error": "", "probing": False}
                )
                self._save_runtime()
            elif action == "up" and index > 0:
                providers[index - 1], providers[index] = providers[index], providers[index - 1]
            elif action == "down" and index < len(providers) - 1:
                providers[index + 1], providers[index] = providers[index], providers[index + 1]
            elif action not in {"up", "down"}:
                if action not in {"toggle", "reset"}:
                    raise ValueError(action)
            for position, item in enumerate(providers):
                item["priority"] = position + 1
            self.settings["providers"] = providers
            atomic_write_json(self.settings_path, self.settings)

    def reset_all(self) -> None:
        with self.lock:
            for state in self.runtime.values():
                state.update({"cooldown_until": 0, "last_error": "", "probing": False})
            self._save_runtime()

    def clear_events(self) -> None:
        with self.lock:
            self.events = []
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            self.events_path.write_text("", encoding="utf-8")
            os.chmod(self.events_path, 0o600)
