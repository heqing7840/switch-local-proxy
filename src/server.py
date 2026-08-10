from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import chain
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlparse

import httpx

from proxy_core import (
    ANTHROPIC_MESSAGES_ADAPTER,
    ProxyStore,
    adapter_for_path,
    estimate_input_tokens,
    inspect_responses_payload,
    inspect_sse_prime,
    is_retryable_status,
    should_retry_same_provider,
)


APP_DIR = Path(__file__).resolve().parent
WEB_FILE = APP_DIR / "web" / "index.html"
LOCALE_DIR = APP_DIR / "web" / "locales"
STATE_DIR = Path(
    os.environ.get(
        "SWITCH_LOCAL_PROXY_STATE_DIR",
        os.environ.get("CODEX_KEY_PROXY_STATE_DIR", "~/.switch-local-proxy"),
    )
).expanduser()
KEY_FILE = Path(
    os.environ.get(
        "SWITCH_LOCAL_PROXY_KEY_FILE",
        os.environ.get("CODEX_KEY_PROXY_KEY_FILE", str(APP_DIR.parent / "key.txt")),
    )
)
HOST = os.environ.get(
    "SWITCH_LOCAL_PROXY_HOST", os.environ.get("CODEX_KEY_PROXY_HOST", "127.0.0.1")
)
PORT = int(
    os.environ.get("SWITCH_LOCAL_PROXY_PORT", os.environ.get("CODEX_KEY_PROXY_PORT", "15722"))
)

STATE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=STATE_DIR / "proxy.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
STORE = ProxyStore(STATE_DIR, KEY_FILE)
CLIENT = httpx.Client(
    timeout=httpx.Timeout(connect=10, read=95, write=30, pool=10),
    limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
    follow_redirects=False,
)

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "origin",
    "proxy-authenticate",
    "proxy-authorization",
    "referer",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
    "authorization",
    "x-api-key",
    "cookie",
}
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
LOCAL_ORIGINS = {"127.0.0.1", "localhost", "::1"}
SECRET_TEXT_RE = re.compile(r"(?i)(?:bearer\s+)?sk-[A-Za-z0-9_-]{8,}")


def compact_error(value: str) -> str:
    compacted = " ".join(value.split())
    return SECRET_TEXT_RE.sub("[redacted-secret]", compacted)[:300]


def header_latin1(value: str) -> str:
    """HTTP 响应头只能 latin-1；中文渠道名等非 ASCII 内容做百分号编码。"""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return quote(value, safe="")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SwitchLocalProxy/0.4"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format: str, *args: object) -> None:
        logging.info("client=%s " + format, self.client_address[0], *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_bytes(200, WEB_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/locales/"):
            locale = parsed.path.removeprefix("/locales/").removesuffix(".json")
            if locale in {"en", "zh-CN"}:
                locale_file = LOCALE_DIR / f"{locale}.json"
                if locale_file.is_file():
                    self._send_bytes(200, locale_file.read_bytes(), "application/json; charset=utf-8")
                    return
            self._send_json(404, {"error": {"message": "Locale not found"}})
            return
        if parsed.path in {"/api/status", "/api/health"}:
            self._send_json(200, STORE.status())
            return
        self._send_json(404, {"error": {"message": "Not found"}})

    def do_POST(self) -> None:
        if not self._request_is_local():
            return
        if not self._content_length_allowed():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/providers":
            body = self._read_json()
            try:
                STORE.create_provider(str(body.get("name", "")), str(body.get("key", "")))
            except ValueError as error:
                self._send_json(400, {"error": {"message": str(error)}})
                return
            except OSError:
                self._send_json(500, {"error": {"message": "密钥文件写入失败"}})
                return
            self._send_json(201, STORE.status())
            return
        if parsed.path == "/api/provider-action":
            body = self._read_json()
            try:
                STORE.provider_action(str(body.get("name", "")), str(body.get("action", "")))
            except (KeyError, ValueError):
                self._send_json(400, {"error": {"message": "Invalid provider action"}})
                return
            self._send_json(200, STORE.status())
            return
        if parsed.path == "/api/reset-all":
            STORE.reset_all()
            self._send_json(200, STORE.status())
            return
        if parsed.path == "/api/clear-events":
            STORE.clear_events()
            self._send_json(200, STORE.status())
            return
        adapter = adapter_for_path(parsed.path)
        if adapter:
            self._proxy_upstream(adapter)
            return
        self._send_json(404, {"error": {"message": "Not found"}})

    def do_PUT(self) -> None:
        if not self._request_is_local():
            return
        if not self._content_length_allowed():
            return
        path = urlparse(self.path).path
        if path == "/api/providers":
            body = self._read_json()
            try:
                STORE.update_provider(
                    str(body.get("old_name", "")),
                    str(body.get("name", "")),
                    str(body.get("key", "")),
                )
            except KeyError:
                self._send_json(404, {"error": {"message": "渠道不存在"}})
                return
            except ValueError as error:
                self._send_json(400, {"error": {"message": str(error)}})
                return
            except OSError:
                self._send_json(500, {"error": {"message": "密钥文件写入失败"}})
                return
            self._send_json(200, STORE.status())
            return
        if path == "/api/settings":
            body = self._read_json()
            try:
                STORE.update_cooldown(int(body.get("cooldown_seconds", 300)))
            except (TypeError, ValueError):
                self._send_json(400, {"error": {"message": "Invalid cooldown"}})
                return
            self._send_json(200, STORE.status())
            return
        self._send_json(404, {"error": {"message": "Not found"}})

    def do_DELETE(self) -> None:
        if not self._request_is_local():
            return
        if not self._content_length_allowed():
            return
        if urlparse(self.path).path != "/api/providers":
            self._send_json(404, {"error": {"message": "Not found"}})
            return
        body = self._read_json()
        try:
            STORE.delete_provider(str(body.get("name", "")))
        except KeyError:
            self._send_json(404, {"error": {"message": "渠道不存在"}})
            return
        except ValueError as error:
            self._send_json(400, {"error": {"message": str(error)}})
            return
        except OSError:
            self._send_json(500, {"error": {"message": "密钥文件写入失败"}})
            return
        self._send_json(200, STORE.status())

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _request_is_local(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        request_host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]").lower()
        origin_host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or (origin_host not in LOCAL_ORIGINS and origin_host != request_host)
        ):
            self._send_json(403, {"error": {"message": "Cross-origin requests are not allowed"}})
            return False
        try:
            allowed_port = parsed.port in {None, PORT}
        except ValueError:
            allowed_port = False
        if not allowed_port:
            self._send_json(403, {"error": {"message": "Cross-origin requests are not allowed"}})
            return False
        return True

    def _content_length_allowed(self) -> bool:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": {"message": "Invalid Content-Length"}})
            return False
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            self._send_json(413, {"error": {"message": "Request body is too large"}})
            return False
        return True

    def _read_json(self) -> dict:
        try:
            value = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _proxy_upstream(self, adapter: str) -> None:
        raw_body = self._read_body()
        try:
            request_json = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "Request body must be JSON"}})
            return
        if not isinstance(request_json, dict):
            self._send_json(400, {"error": {"message": "Request body must be an object"}})
            return

        requested_model = str(request_json.get("model") or "")
        upstream_model = str(STORE.settings["forced_model"])
        request_json["model"] = upstream_model
        payload = json.dumps(request_json, ensure_ascii=False, separators=(",", ":")).encode()
        request_id = f"{time.time_ns():x}"
        input_tokens_estimate = estimate_input_tokens(payload)
        upstream_path = self.path
        upstream_url = STORE.settings["upstream_base_url"].rstrip("/") + upstream_path[3:]
        providers = STORE.eligible_providers()
        if not providers:
            STORE.set_active_provider(None)
            self._send_all_unavailable([])
            return

        failures: list[dict[str, str]] = []
        for index, provider in enumerate(providers):
            STORE.set_active_provider(provider["name"])
            retry_count = 0
            while True:
                started = time.monotonic()
                try:
                    with CLIENT.stream(
                        "POST",
                        upstream_url,
                        headers=self._upstream_headers(provider["key"], adapter),
                        content=payload,
                    ) as response:
                        latency_ms = int((time.monotonic() - started) * 1000)
                        if should_retry_same_provider(response.status_code, retry_count):
                            error_body = response.read()[:8192]
                            retry_count += 1
                            logging.warning(
                                "provider=%s status=502 same_provider_retry=%s error=%s",
                                provider["name"],
                                retry_count,
                                self._upstream_error(response.status_code, error_body),
                            )
                            time.sleep(1)
                            continue
                        if is_retryable_status(response.status_code):
                            error_body = response.read()[:8192]
                            message = self._upstream_error(response.status_code, error_body)
                            STORE.mark_failure(
                                provider["name"], message, latency_ms, requested_model, upstream_model,
                                request_id, input_tokens_estimate, retry_count, adapter
                            )
                            failures.append({"provider": provider["name"], "error": message})
                            logging.warning("provider=%s failure=%s", provider["name"], message)
                            break
                        if response.status_code >= 400:
                            body = response.read()
                            STORE.release_untried_probe(provider)
                            self._send_upstream_buffered(response, body, provider["name"])
                            self._release_remaining_probes(providers[index + 1 :])
                            return

                        content_type = response.headers.get("content-type", "").lower()
                        if "text/event-stream" in content_type or request_json.get("stream") is True:
                            outcome = self._send_stream(
                                response, provider, latency_ms, requested_model, upstream_model,
                                request_id, input_tokens_estimate, retry_count, adapter
                            )
                            if outcome == "retry":
                                failures.append(
                                    {
                                        "provider": provider["name"],
                                        "error": STORE.runtime[provider["name"]]["last_error"],
                                    }
                                )
                                break
                            self._release_remaining_probes(providers[index + 1 :])
                            return

                        body = response.read()
                        semantic_error = inspect_responses_payload(body)
                        if semantic_error:
                            message = compact_error(semantic_error)
                            STORE.mark_failure(
                                provider["name"], message, latency_ms, requested_model, upstream_model,
                                request_id, input_tokens_estimate, retry_count, adapter
                            )
                            failures.append({"provider": provider["name"], "error": message})
                            break
                        STORE.mark_success(
                            provider["name"], latency_ms, requested_model, upstream_model,
                            request_id, input_tokens_estimate, retry_count, adapter
                        )
                        self._send_upstream_buffered(response, body, provider["name"])
                        self._release_remaining_probes(providers[index + 1 :])
                        return
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
                    message = compact_error(type(error).__name__ + ": " + str(error))
                    latency_ms = int((time.monotonic() - started) * 1000)
                    STORE.mark_failure(
                        provider["name"], message, latency_ms, requested_model, upstream_model,
                        request_id, input_tokens_estimate, retry_count, adapter
                    )
                    failures.append({"provider": provider["name"], "error": message})
                    logging.warning("provider=%s failure=%s", provider["name"], message)
                    break
                except Exception as error:
                    message = compact_error(type(error).__name__ + ": " + str(error))
                    latency_ms = int((time.monotonic() - started) * 1000)
                    STORE.mark_failure(
                        provider["name"], message, latency_ms, requested_model, upstream_model,
                        request_id, input_tokens_estimate, retry_count, adapter
                    )
                    failures.append({"provider": provider["name"], "error": message})
                    logging.exception("provider=%s unexpected failure", provider["name"])
                    break

        STORE.set_active_provider(None)
        self._send_all_unavailable(failures)

    def _send_stream(
        self,
        response: httpx.Response,
        provider: dict,
        latency_ms: int,
        requested_model: str,
        upstream_model: str,
        request_id: str,
        input_tokens_estimate: int,
        retry_count: int,
        adapter: str,
    ) -> str:
        iterator = response.iter_bytes()
        prime: list[bytes] = []
        prime_size = 0
        try:
            while prime_size < 256 * 1024:
                chunk = next(iterator)
                if not chunk:
                    continue
                prime.append(chunk)
                prime_size += len(chunk)
                outcome, message = inspect_sse_prime(b"".join(prime))
                if outcome == "error":
                    error = compact_error(message or "Upstream SSE failure")
                    STORE.mark_failure(
                        provider["name"], error, latency_ms, requested_model, upstream_model,
                        request_id, input_tokens_estimate, retry_count, adapter
                    )
                    logging.warning("provider=%s semantic_failure=%s", provider["name"], error)
                    return "retry"
                if outcome == "productive":
                    break
        except StopIteration:
            combined = b"".join(prime)
            semantic_error = inspect_responses_payload(combined)
            if semantic_error:
                STORE.mark_failure(
                    provider["name"],
                    compact_error(semantic_error),
                    latency_ms,
                    requested_model,
                    upstream_model,
                    request_id,
                    input_tokens_estimate,
                    retry_count,
                    adapter,
                )
                return "retry"
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
            STORE.mark_failure(
                provider["name"], compact_error(str(error)), latency_ms, requested_model, upstream_model,
                request_id, input_tokens_estimate, retry_count, adapter
            )
            return "retry"

        self.send_response(response.status_code)
        self._copy_response_headers(response, streaming=True)
        self.send_header("Transfer-Encoding", "chunked")
        # 渠道名常含中文，必须先转为 latin-1 安全值再写入响应头
        self.send_header("X-Switch-Local-Proxy-Provider", header_latin1(provider["name"]))
        self.send_header("X-Codex-Key-Provider", header_latin1(provider["name"]))
        self.end_headers()
        STORE.mark_success(
            provider["name"], latency_ms, requested_model, upstream_model,
            request_id, input_tokens_estimate, retry_count, adapter
        )
        try:
            for chunk in chain(prime, iterator):
                if chunk:
                    self._write_chunk(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logging.info("client disconnected provider=%s", provider["name"])
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
            STORE.mark_failure(
                provider["name"],
                compact_error("stream: " + str(error)),
                latency_ms,
                requested_model,
                upstream_model,
                request_id,
                input_tokens_estimate,
                retry_count,
                adapter,
            )
            logging.warning("provider=%s stream_failure=%s", provider["name"], error)
        return "sent"

    def _write_chunk(self, chunk: bytes) -> None:
        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _upstream_headers(self, api_key: str, adapter: str) -> dict[str, str]:
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
        }
        headers["Authorization"] = f"Bearer {api_key}"
        if adapter == ANTHROPIC_MESSAGES_ADAPTER:
            headers["X-Api-Key"] = api_key
            if not any(name.lower() == "anthropic-version" for name in headers):
                headers["anthropic-version"] = "2023-06-01"
        headers["Content-Type"] = "application/json"
        headers["Accept-Encoding"] = "identity"
        return headers

    @staticmethod
    def _upstream_error(status_code: int, body: bytes) -> str:
        semantic = inspect_responses_payload(body)
        return compact_error(semantic or f"Upstream HTTP {status_code}")

    def _send_upstream_buffered(
        self, response: httpx.Response, body: bytes, provider_name: str
    ) -> None:
        self.send_response(response.status_code)
        self._copy_response_headers(response, streaming=False)
        self.send_header("X-Switch-Local-Proxy-Provider", header_latin1(provider_name))
        self.send_header("X-Codex-Key-Provider", header_latin1(provider_name))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _copy_response_headers(self, response: httpx.Response, streaming: bool) -> None:
        for name, value in response.headers.items():
            lowered = name.lower()
            if lowered in HOP_BY_HOP_HEADERS or (streaming and lowered == "content-length"):
                continue
            self.send_header(name, header_latin1(value))

    def _release_remaining_probes(self, providers: Iterable[dict]) -> None:
        for provider in providers:
            STORE.release_untried_probe(provider)

    def _send_all_unavailable(self, failures: list[dict[str, str]]) -> None:
        status = STORE.status()
        next_retry = min(
            (
                item["cooldown_remaining"]
                for item in status["providers"]
                if item["cooldown_remaining"] > 0
            ),
            default=0,
        )
        self._send_json(
            503,
            {
                "error": {
                    "message": "All providers are unavailable or cooling down",
                    "type": "local_failover_unavailable",
                    "next_retry_seconds": next_retry,
                    "failures": failures,
                }
            },
        )

    def _send_json(self, status: int, value: dict) -> None:
        self._send_bytes(
            status,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
        )

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    server = ReusableThreadingHTTPServer((HOST, PORT), Handler)
    logging.info("service started host=%s port=%s hostname=%s", HOST, PORT, socket.gethostname())
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        CLIENT.close()
        server.server_close()


if __name__ == "__main__":
    main()
