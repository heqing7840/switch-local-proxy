import json
import http.client
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proxy_core import (  # noqa: E402
    ANTHROPIC_MESSAGES_ADAPTER,
    DEFAULT_PROVIDER_NAMES,
    OPENAI_CHAT_COMPLETIONS_ADAPTER,
    OPENAI_RESPONSES_ADAPTER,
    ProxyStore,
    adapter_for_path,
    estimate_input_tokens,
    inspect_responses_payload,
    inspect_sse_prime,
    is_context_error,
    is_retryable_status,
    parse_key_text,
    parse_private_settings,
    source_ip_allowed,
    should_retry_same_provider,
)
import server  # noqa: E402
from server import (  # noqa: E402
    MAX_REQUEST_BODY_BYTES,
    Handler,
    UpdateChecker,
    compact_error,
    header_latin1,
)


class ProxyCoreTests(unittest.TestCase):
    def test_update_status_contains_only_public_version_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "version.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": "0.5.1",
                        "release_url": "https://github.com/example/project/commits/main",
                    }
                ),
                encoding="utf-8",
            )
            checker = UpdateChecker(manifest, root / "state")
            checker.cache = {
                "state": "ok",
                "latest_version": "0.5.2",
                "checked_at": 123.0,
            }

            status = checker._public_status()

            self.assertEqual(status["state"], "available")
            self.assertEqual(status["current_version"], "0.5.1")
            self.assertEqual(status["latest_version"], "0.5.2")
            self.assertEqual(
                status["release_url"],
                "https://github.com/example/project/commits/main",
            )
            self.assertEqual(
                set(status),
                {"state", "current_version", "latest_version", "release_url", "checked_at"},
            )

    def test_supported_adapter_paths_are_explicit(self):
        self.assertEqual(adapter_for_path("/v1/responses"), OPENAI_RESPONSES_ADAPTER)
        self.assertEqual(
            adapter_for_path("/v1/chat/completions"), OPENAI_CHAT_COMPLETIONS_ADAPTER
        )
        self.assertEqual(adapter_for_path("/v1/messages"), ANTHROPIC_MESSAGES_ADAPTER)
        self.assertEqual(
            adapter_for_path("/v1/messages/count_tokens"), ANTHROPIC_MESSAGES_ADAPTER
        )
        self.assertIsNone(adapter_for_path("/v1beta/models/gemini:generateContent"))

    def test_anthropic_messages_and_count_tokens_use_provider_credentials(self):
        class AnthropicUpstream(Handler):
            requests = []

            def log_message(self, format, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                type(self).requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "x_api_key": self.headers.get("X-Api-Key"),
                        "anthropic_version": self.headers.get("anthropic-version"),
                        "anthropic_beta": self.headers.get("anthropic-beta"),
                        "cookie": self.headers.get("Cookie"),
                        "referer": self.headers.get("Referer"),
                        "body": json.loads(body),
                    }
                )
                if self.path == "/v1/messages/count_tokens":
                    response_body = b'{"input_tokens":12}'
                else:
                    response_body = (
                        b'{"id":"msg_test","type":"message","role":"assistant",'
                        b'"content":[{"type":"text","text":"ok"}],"model":"gpt-5.6-sol",'
                        b'"stop_reason":"end_turn","usage":{"input_tokens":4,"output_tokens":1}}'
                    )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("claude-route:sk-upstream-provider\n", encoding="utf-8")
            store = ProxyStore(root / "state", key_file)
            store.settings["forced_model"] = "gpt-5.6-sol"
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicUpstream)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            store.settings["upstream_base_url"] = (
                f"http://127.0.0.1:{upstream.server_port}/v1"
            )

            proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            previous_store = server.STORE
            server.STORE = store
            proxy_thread.start()
            try:
                for path in ("/v1/messages", "/v1/messages/count_tokens"):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", proxy.server_port, timeout=4
                    )
                    connection.request(
                        "POST",
                        path,
                        body=json.dumps(
                            {
                                "model": "claude-sonnet-4-6",
                                "max_tokens": 32,
                                "messages": [{"role": "user", "content": "hello"}],
                            }
                        ),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer local-only-token",
                            "X-Api-Key": "local-only-key",
                            "Anthropic-Version": "2023-06-01",
                            "anthropic-beta": "test-feature",
                            "Cookie": "local-session=must-not-leave-loopback",
                            "Referer": "http://127.0.0.1:15722/",
                        },
                    )
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.getheader("X-Switch-Local-Proxy-Provider"),
                        "claude-route",
                    )
                    connection.close()

                self.assertEqual(
                    [item["path"] for item in AnthropicUpstream.requests],
                    ["/v1/messages", "/v1/messages/count_tokens"],
                )
                for request in AnthropicUpstream.requests:
                    self.assertEqual(
                        request["authorization"], "Bearer sk-upstream-provider"
                    )
                    self.assertEqual(request["x_api_key"], "sk-upstream-provider")
                    self.assertEqual(request["anthropic_version"], "2023-06-01")
                    self.assertEqual(request["anthropic_beta"], "test-feature")
                    self.assertIsNone(request["cookie"])
                    self.assertIsNone(request["referer"])
                    self.assertEqual(request["body"]["model"], "claude-sonnet-4-6")
                events = store.status()["events"]
                self.assertEqual(events[0]["adapter"], ANTHROPIC_MESSAGES_ADAPTER)
                self.assertEqual(events[0]["requested_model"], "claude-sonnet-4-6")
                self.assertEqual(events[0]["model"], "claude-sonnet-4-6")
            finally:
                proxy.shutdown()
                proxy.server_close()
                proxy_thread.join(timeout=2)
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=2)
                server.STORE = previous_store

    def test_502_retries_same_provider_before_failover(self):
        class FlakyUpstream(Handler):
            calls = 0

            def log_message(self, format, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                type(self).calls += 1
                if type(self).calls == 1:
                    body = b'{"error":{"message":"temporary gateway failure"}}'
                    self.send_response(502)
                else:
                    body = b'{"id":"response-ok","status":"completed"}'
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("测试渠道:sk-test-provider\n", encoding="utf-8")
            store = ProxyStore(root / "state", key_file)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), FlakyUpstream)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            store.settings["upstream_base_url"] = f"http://127.0.0.1:{upstream.server_port}/v1"

            proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            previous_store = server.STORE
            server.STORE = store
            proxy_thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=4)
            try:
                payload = json.dumps({"model": "gpt-5.6-sol", "input": "hello"})
                connection.request(
                    "POST",
                    "/v1/responses",
                    body=payload,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(FlakyUpstream.calls, 2)
                provider = store.status()["providers"][0]
                self.assertEqual(provider["success_count"], 1)
                self.assertEqual(provider["failure_count"], 0)
                self.assertEqual(store.status()["events"][0]["retry_count"], 1)
            finally:
                connection.close()
                proxy.shutdown()
                proxy.server_close()
                proxy_thread.join(timeout=2)
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=2)
                server.STORE = previous_store

    def test_context_estimate_and_classification(self):
        estimate = estimate_input_tokens(json.dumps({"input": "a" * 4000}).encode())
        self.assertGreaterEqual(estimate, 1000)
        with_image = estimate_input_tokens(json.dumps({
            "input": "a" * 4000,
            "image_url": "data:image/png;base64," + "A" * 400_000,
        }).encode())
        self.assertLess(with_image, 2000)
        self.assertTrue(is_context_error("Your input exceeds the context window"))
        self.assertFalse(is_context_error("Upstream service temporarily unavailable"))

    def test_only_first_502_retries_same_provider(self):
        self.assertTrue(should_retry_same_provider(502, 0))
        self.assertFalse(should_retry_same_provider(502, 1))
        self.assertFalse(should_retry_same_provider(503, 0))

    def test_header_latin1_encodes_chinese_provider_names(self):
        name = "codex-app-5-速刷"
        safe = header_latin1(name)
        safe.encode("latin-1")
        self.assertNotEqual(safe, name)
        self.assertEqual(header_latin1("ascii-only"), "ascii-only")
    def test_key_parser_only_accepts_codex_provider_lines(self):
        parsed = parse_key_text(
            "password: do-not-use\n"
            "codex-app-5-速刷:sk-test-one\n"
            "codex-app-6-plus0.28倍: sk-test-two\n"
        )
        self.assertEqual(
            parsed,
            {
                "codex-app-5-速刷": "sk-test-one",
                "codex-app-6-plus0.28倍": "sk-test-two",
            },
        )

    def test_private_settings_are_parsed_without_becoming_providers(self):
        text = (
            "upstream_url:https://api.example.com/v1\n"
            "forced_model:model-name\n"
            "primary:sk-test-provider\n"
        )
        self.assertEqual(
            parse_private_settings(text),
            {
                "upstream_url": "https://api.example.com/v1",
                "forced_model": "model-name",
            },
        )
        self.assertEqual(parse_key_text(text), {"primary": "sk-test-provider"})

    def test_private_key_file_settings_bootstrap_a_fresh_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text(
                "upstream_url:https://api.example.com/v1\n"
                "forced_model:model-name\n"
                "primary:sk-test-provider\n",
                encoding="utf-8",
            )
            store = ProxyStore(root / "state", key_file)
            self.assertTrue(store.status()["ok"])
            self.assertEqual(store.settings["upstream_base_url"], "https://api.example.com/v1")
            self.assertEqual(store.settings["forced_model"], "model-name")
            store.create_provider("secondary", "sk-secondary-key")
            rewritten = key_file.read_text(encoding="utf-8")
            self.assertIn("upstream_url:https://api.example.com/v1", rewritten)
            self.assertIn("forced_model:model-name", rewritten)

    def test_sse_capacity_failure_is_retryable(self):
        payload = (
            b'event: response.created\ndata: {"type":"response.created"}\n\n'
            b'event: response.failed\ndata: {"type":"response.failed",'
            b'"response":{"status":"failed","error":{"message":"at capacity"}}}\n\n'
        )
        outcome, message = inspect_sse_prime(payload)
        self.assertEqual(outcome, "error")
        self.assertEqual(message, "at capacity")

    def test_productive_sse_is_committable(self):
        outcome, _ = inspect_sse_prime(
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        )
        self.assertEqual(outcome, "productive")

    def test_anthropic_sse_delta_is_productive_and_error_is_retryable(self):
        productive, _ = inspect_sse_prime(
            b'event: content_block_delta\ndata: {"type":"content_block_delta",'
            b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
        )
        self.assertEqual(productive, "productive")
        outcome, message = inspect_sse_prime(
            b'event: error\ndata: {"type":"error",'
            b'"error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
        )
        self.assertEqual(outcome, "error")
        self.assertEqual(message, "Overloaded")
        ordinary_text, _ = inspect_sse_prime(
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",'
            b'"delta":"ordinary text containing event: error"}\n\n'
        )
        self.assertEqual(ordinary_text, "productive")

    def test_openai_chat_completions_sse_is_productive(self):
        outcome, _ = inspect_sse_prime(
            b'data: {"id":"chatcmpl_test","choices":[{"delta":{"content":"ok"},"index":0}]}'
            b"\n\n"
        )
        self.assertEqual(outcome, "productive")

    def test_error_compaction_redacts_provider_credentials(self):
        redacted = compact_error("upstream echoed Bearer sk-sensitive-provider-key")
        self.assertNotIn("sk-sensitive-provider-key", redacted)
        self.assertIn("[redacted-secret]", redacted)

    def test_context_failure_is_recorded_without_cooling_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("provider:sk-context-test-key\n", encoding="utf-8")
            store = ProxyStore(root / "state", key_file)
            store.mark_failure(
                "provider",
                "Your input exceeds the context window of this model.",
                input_tokens_estimate=140000,
            )
            state = store.runtime["provider"]
            self.assertEqual(state["cooldown_until"], 0)
            self.assertEqual(state["failure_count"], 1)
            self.assertEqual(state["context_failure_count"], 1)

    def test_existing_context_cooldown_is_cleared_on_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("provider:sk-context-test-key\n", encoding="utf-8")
            store = ProxyStore(root / "state", key_file)
            store.runtime_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "cooldown_until": 9999999999,
                            "last_error": "Your input exceeds the context window of this model.",
                        }
                    }
                ),
                encoding="utf-8",
            )
            reloaded = ProxyStore(root / "state", key_file)
            self.assertEqual(reloaded.runtime["provider"]["cooldown_until"], 0)

    def test_key_file_errors_do_not_expose_local_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "private" / "key.txt"
            store = ProxyStore(Path(directory) / "state", key_file)
            status = store.status()
            self.assertIsNone(status["key_error"])
            self.assertNotIn(str(key_file), json.dumps(status))

    def test_json_error_envelope_is_detected(self):
        self.assertEqual(
            inspect_responses_payload(b'{"error":{"message":"unavailable"}}'),
            "unavailable",
        )

    def test_retry_statuses(self):
        self.assertTrue(is_retryable_status(429))
        self.assertTrue(is_retryable_status(503))
        self.assertFalse(is_retryable_status(400))

    def test_failure_cools_provider_and_settings_do_not_store_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text(
                "\n".join(f"{name}:sk-secret-{index}" for index, name in enumerate(DEFAULT_PROVIDER_NAMES)),
                encoding="utf-8",
            )
            store = ProxyStore(root / "state", key_file)
            name = DEFAULT_PROVIDER_NAMES[0]
            store.mark_failure(name, "HTTP 503")
            status = store.status()
            first = status["providers"][0]
            self.assertEqual(first["health"], "cooling")
            self.assertGreater(first["cooldown_remaining"], 0)
            with sqlite3.connect(root / "state" / "proxy.sqlite3") as connection:
                persisted = json.dumps(
                    connection.execute(
                        "SELECT value FROM proxy_data WHERE name != 'keys'"
                    ).fetchall()
                )
            self.assertNotIn("sk-secret", persisted)
            events = store.status()["events"]
            self.assertEqual(events[0]["result"], "failure")
            self.assertEqual(events[0]["provider"], name)

    def test_reset_and_priority_actions_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text(
                "\n".join(f"{name}:sk-test-{index}" for index, name in enumerate(DEFAULT_PROVIDER_NAMES)),
                encoding="utf-8",
            )
            store = ProxyStore(root / "state", key_file)
            second = DEFAULT_PROVIDER_NAMES[1]
            store.provider_action(second, "up")
            self.assertEqual(store.status()["providers"][0]["name"], second)
            store.mark_failure(second, "HTTP 503")
            store.provider_action(second, "reset")
            self.assertEqual(store.status()["providers"][0]["health"], "ready")

    def test_events_persist_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text(
                "\n".join(f"{name}:sk-test-{index}" for index, name in enumerate(DEFAULT_PROVIDER_NAMES)),
                encoding="utf-8",
            )
            state_dir = root / "state"
            store = ProxyStore(state_dir, key_file)
            store.mark_success(
                DEFAULT_PROVIDER_NAMES[0],
                1234,
                requested_model="gpt-5.6-luna",
                upstream_model="gpt-5.6-sol",
                request_id="request-one",
                input_tokens_estimate=145000,
                retry_count=1,
            )
            reloaded = ProxyStore(state_dir, key_file)
            event = reloaded.status()["events"][0]
            self.assertEqual(event["latency_ms"], 1234)
            self.assertEqual(event["requested_model"], "gpt-5.6-luna")
            self.assertEqual(event["model"], "gpt-5.6-sol")
            self.assertEqual(event["request_id"], "request-one")
            self.assertEqual(event["input_tokens_estimate"], 145000)
            self.assertEqual(event["input_estimator_version"], 2)
            self.assertEqual(event["retry_count"], 1)
            provider = reloaded.status()["providers"][0]
            self.assertEqual(provider["max_success_input_tokens"], 145000)
            reloaded.mark_failure(
                DEFAULT_PROVIDER_NAMES[0],
                "Your input exceeds the context window",
                input_tokens_estimate=160000,
                request_id="request-two",
            )
            provider = reloaded.status()["providers"][0]
            self.assertEqual(provider["min_context_failure_tokens"], 160000)
            reloaded.clear_events()
            self.assertEqual(reloaded.status()["events"], [])

    def test_dynamic_providers_are_loaded_from_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text(
                "低价渠道:sk-dynamic-low\n快速渠道:sk-dynamic-fast\n",
                encoding="utf-8",
            )
            store = ProxyStore(root / "state", key_file)
            self.assertEqual(
                [item["name"] for item in store.status()["providers"]],
                ["低价渠道", "快速渠道"],
            )

    def test_provider_specific_upstream_model_and_protocol_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "credentials.txt"
            key_file.write_text("responses:sk-responses-provider\nchat:sk-chat-provider\n", encoding="utf-8")
            store = ProxyStore(root / "state", key_file)
            store.update_provider(
                "responses",
                "responses",
                upstream_url="https://responses.example.invalid/v1",
                forced_model="responses-model",
                protocol=OPENAI_RESPONSES_ADAPTER,
            )
            store.update_provider(
                "chat",
                "chat",
                upstream_url="https://chat.example.invalid/v1beta/openai",
                forced_model="chat-model",
                protocol=OPENAI_CHAT_COMPLETIONS_ADAPTER,
            )

            responses = store.eligible_providers(OPENAI_RESPONSES_ADAPTER)
            chat = store.eligible_providers(OPENAI_CHAT_COMPLETIONS_ADAPTER)
            self.assertEqual([item["name"] for item in responses], ["responses"])
            self.assertEqual([item["name"] for item in chat], ["chat"])
            self.assertEqual(store.provider_model(responses[0]), "responses-model")
            self.assertEqual(
                store.provider_upstream_url(chat[0]),
                "https://chat.example.invalid/v1beta/openai",
            )

    def test_source_ip_policies_and_channel_override(self):
        self.assertTrue(source_ip_allowed("127.0.0.1", "local"))
        self.assertFalse(source_ip_allowed("192.168.1.20", "local"))
        self.assertTrue(source_ip_allowed("192.168.1.20", "lan"))
        self.assertTrue(source_ip_allowed("2.2.2.2", "cidr", "2.2.2.0/24"))
        self.assertFalse(source_ip_allowed("2.2.3.2", "cidr", "2.2.2.0/24"))
        self.assertTrue(source_ip_allowed("203.0.113.8", "all"))

        with tempfile.TemporaryDirectory() as directory:
            store = ProxyStore(Path(directory) / "state")
            store.update_access("all")
            store.create_provider(
                "inherits-global",
                "sk-inherit-provider-key",
                upstream_url="https://api.example.invalid/v1",
            )
            store.create_provider(
                "local-override",
                "sk-local-provider-key",
                upstream_url="https://api.example.invalid/v1",
                access_policy="local",
            )
            remote = store.eligible_providers(OPENAI_RESPONSES_ADAPTER, "203.0.113.8")
            self.assertEqual([item["name"] for item in remote], ["inherits-global"])

    def test_cidr_access_requires_valid_one_rule_per_line(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProxyStore(Path(directory) / "state")
            store.create_provider(
                "cidr-provider",
                "sk-cidr-provider-key",
                upstream_url="https://api.example.invalid/v1",
                access_policy="cidr",
                allowed_networks="2.2.2.0/24\n2001:db8::/32",
            )
            provider = store.status()["providers"][0]
            self.assertEqual(provider["allowed_networks"], "2.2.2.0/24, 2001:db8::/32")
            with self.assertRaises(ValueError):
                store.update_access("cidr", "not-an-ip")

    def test_new_channel_uses_client_model_when_no_override_is_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProxyStore(Path(directory) / "state")
            store.create_provider(
                "new-provider",
                "sk-new-provider-key",
                upstream_url="https://api.example.invalid/v1",
            )
            provider = store.eligible_providers(OPENAI_RESPONSES_ADAPTER)[0]
            self.assertEqual(store.provider_model(provider), "")
            self.assertTrue(store.status()["ok"])

    def test_global_gpt_guard_rewrites_luna_without_touching_other_families(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProxyStore(Path(directory) / "state")
            store.create_provider(
                "multi-model-provider",
                "sk-multi-model-key",
                upstream_url="https://api.example.invalid/v1",
            )
            provider = store.eligible_providers(OPENAI_RESPONSES_ADAPTER)[0]
            self.assertEqual(
                store.provider_model(provider, "gpt-5.6-luna"),
                "gpt-5.6-sol",
            )
            self.assertEqual(store.provider_model(provider, "grok-4"), "")
            self.assertEqual(store.provider_model(provider, "gemini-2.5-pro"), "")
            store.update_provider(
                "multi-model-provider",
                "multi-model-provider",
                model_policy="client",
            )
            provider = store.eligible_providers(OPENAI_RESPONSES_ADAPTER)[0]
            self.assertEqual(store.provider_model(provider, "gpt-5.6-luna"), "")

    def test_legacy_global_model_is_migrated_to_existing_channels_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "credentials.txt"
            source.write_text(
                "upstream_url:https://api.example.invalid/v1\n"
                "forced_model:legacy-model\n"
                "existing:sk-existing-provider-key\n",
                encoding="utf-8",
            )
            store = ProxyStore(root / "state", source)
            existing = store.eligible_providers(OPENAI_RESPONSES_ADAPTER)[0]
            self.assertEqual(store.provider_model(existing), "legacy-model")
            store.create_provider(
                "new-provider",
                "sk-new-provider-key",
                upstream_url="https://api.example.invalid/v1",
            )
            new_provider = next(
                item for item in store.eligible_providers(OPENAI_RESPONSES_ADAPTER)
                if item["name"] == "new-provider"
            )
            self.assertEqual(store.provider_model(new_provider), "")

    def test_provider_create_update_and_delete_never_exposes_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("现有渠道:sk-existing-value\n", encoding="utf-8")
            state_dir = root / "state"
            store = ProxyStore(state_dir, key_file)

            store.create_provider("新增渠道", "sk-created-value")
            status_text = json.dumps(store.status(), ensure_ascii=False)
            self.assertNotIn("sk-created-value", status_text)
            provider = next(
                item for item in store.status()["providers"]
                if item["name"] == "新增渠道"
            )
            self.assertTrue(provider["has_key"])
            self.assertEqual(provider["key_hint"], "sk-cre…alue")

            store.provider_action("新增渠道", "up")
            store.mark_success("新增渠道", 250)
            store.update_provider("新增渠道", "更新渠道", "")
            self.assertEqual(store.status()["providers"][0]["name"], "更新渠道")
            self.assertEqual(store.status()["current_provider"], "更新渠道")
            self.assertEqual(store.load_keys()["更新渠道"], "sk-created-value")

            store.delete_provider("更新渠道")
            self.assertNotIn("更新渠道", store.load_keys())
            self.assertNotIn(
                "更新渠道", [item["name"] for item in store.status()["providers"]]
            )

    def test_deleting_all_providers_does_not_restore_hardcoded_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("仅有渠道:sk-only-provider\n", encoding="utf-8")
            state_dir = root / "state"
            store = ProxyStore(state_dir, key_file)
            store.delete_provider("仅有渠道")
            reloaded = ProxyStore(state_dir, key_file)
            self.assertEqual(reloaded.status()["providers"], [])

    def test_invalid_persisted_numbers_fall_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("容错渠道:sk-valid-provider\n", encoding="utf-8")
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "settings.json").write_text(
                json.dumps({
                    "cooldown_seconds": "错误",
                    "providers": [{"name": "容错渠道", "priority": "错误"}],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (state_dir / "runtime.json").write_text(
                json.dumps({"容错渠道": {
                    "cooldown_until": "错误",
                    "success_count": "错误",
                    "failure_count": None,
                }}, ensure_ascii=False),
                encoding="utf-8",
            )
            store = ProxyStore(state_dir, key_file)
            self.assertEqual(store.settings["cooldown_seconds"], 300)
            self.assertEqual(store.status()["providers"][0]["success_count"], 0)

    def test_old_context_estimates_are_reset_on_estimator_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("升级渠道:sk-valid-provider\n", encoding="utf-8")
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "settings.json").write_text(
                json.dumps({"providers": [{"name": "升级渠道", "priority": 1}]}),
                encoding="utf-8",
            )
            (state_dir / "runtime.json").write_text(
                json.dumps({"升级渠道": {
                    "success_count": 8,
                    "max_success_input_tokens": 1450000,
                    "context_success_count": 4,
                }}),
                encoding="utf-8",
            )
            provider = ProxyStore(state_dir, key_file).status()["providers"][0]
            self.assertEqual(provider["success_count"], 8)
            self.assertIsNone(provider["max_success_input_tokens"])
            self.assertEqual(provider["context_success_count"], 0)

    def test_provider_management_http_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("现有渠道:sk-existing-api\n", encoding="utf-8")
            store = ProxyStore(root / "state", key_file)
            previous_store = server.STORE
            server.STORE = store
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()

            def request(method, payload):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", httpd.server_port, timeout=2
                )
                connection.request(
                    method,
                    "/api/providers",
                    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                data = json.loads(response.read())
                connection.close()
                return response.status, data

            try:
                status, data = request(
                    "POST", {"name": "API 新增", "key": "sk-api-created"}
                )
                self.assertEqual(status, 201)
                self.assertNotIn("sk-api-created", json.dumps(data, ensure_ascii=False))

                status, data = request(
                    "PUT", {"old_name": "API 新增", "name": "API 修改", "key": ""}
                )
                self.assertEqual(status, 200)
                self.assertIn("API 修改", [item["name"] for item in data["providers"]])

                status, data = request("DELETE", {"name": "API 修改"})
                self.assertEqual(status, 200)
                self.assertNotIn("API 修改", [item["name"] for item in data["providers"]])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                server.STORE = previous_store

    def test_cross_origin_mutation_is_rejected_before_provider_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("existing:sk-existing-api\n", encoding="utf-8")
            store = ProxyStore(root / "state", key_file)
            previous_store = server.STORE
            server.STORE = store
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_port, timeout=2
            )
            try:
                connection.request(
                    "POST",
                    "/api/providers",
                    body=json.dumps({"name": "blocked", "key": "sk-blocked-api"}),
                    headers={
                        "Content-Type": "text/plain",
                        "Origin": "https://attacker.invalid",
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                self.assertNotIn("blocked", store.load_keys())
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                server.STORE = previous_store

    def test_wrong_loopback_origin_port_is_rejected_with_response(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        try:
            connection.request(
                "POST",
                "/api/reset-all",
                body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Origin": "http://127.0.0.1:65534",
                },
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_matching_loopback_alias_origin_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "key.txt"
            key_file.write_text("existing:sk-existing-api\n", encoding="utf-8")
            store = ProxyStore(root / "state", key_file)
            previous_store = server.STORE
            server.STORE = store
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_port, timeout=2
            )
            try:
                connection.request(
                    "POST",
                    "/api/reset-all",
                    body=b"{}",
                    headers={
                        "Host": "local-proxy.invalid:15722",
                        "Origin": "http://local-proxy.invalid:15722",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                server.STORE = previous_store

    def test_oversized_request_is_rejected_before_body_read(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        try:
            connection.putrequest("POST", "/v1/responses")
            connection.putheader("Content-Length", str(MAX_REQUEST_BODY_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 413)
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_english_locale_is_served_with_new_brand(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        try:
            connection.request("GET", "/locales/en.json")
            response = connection.getresponse()
            data = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(data["brandName"], "Switch Local Proxy")
            self.assertEqual(data["language.simplifiedChinese"], "简体中文")
            self.assertEqual(
                data["dialog.protocolSupport"],
            "Supports GPT/Codex and Grok (OpenAI Responses), Gemini through an "
            "OpenAI-compatible Chat Completions endpoint, and Claude (Anthropic "
            "Messages). Other protocols are not supported.",
            )
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
