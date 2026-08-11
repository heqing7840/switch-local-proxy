# Switch Local Proxy

[简体中文](README.zh-CN.md)

A local AI API proxy with prioritized key routing, automatic failover, retries, cooldowns, and real-time provider monitoring.

The proxy accepts OpenAI Responses, OpenAI-compatible Chat Completions, and Anthropic Messages-compatible requests. All adapters share the same provider priority, health, retry, cooldown, and monitoring model.

## Features

- Priority-based routing across multiple API keys.
- One same-provider retry for an HTTP 502 before failover.
- Temporary cooldowns with automatic recovery probes.
- Streaming SSE early-error detection before forwarding output.
- OpenAI Responses, Chat Completions, and Anthropic Messages pass-through adapters.
- Local dashboard for provider status, latency, failures, key hints, model routing, and request history.
- Add, edit, delete, enable, disable, reorder, and restore providers from the dashboard.
- No database or cloud control plane. Runtime state stays on the local machine.
- The service binds to loopback by default and runs as a macOS background LaunchAgent.
- Cross-origin browser writes are rejected, request bodies are capped, and client cookies are never forwarded upstream.
- The release gate scans tracked files, new unignored files, all Git history, and the `dist/` allowlist without printing matched values.

## Requirements

- macOS with `launchctl`.
- Python 3.11 or newer.
- An upstream API endpoint compatible with the selected adapter.

## Quick Start

```bash
git clone https://github.com/heqing7840/switch-local-proxy.git
cd switch-local-proxy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create the ignored local `key.txt` file. It must never be committed:

```text
upstream_url:<your-upstream-api-base>/v1
forced_model:<your-model-name>
provider-one:<your-api-key>
provider-two:<your-api-key>
```

Then run:

```bash
chmod 600 key.txt
./run.sh verify
./run.sh install
./run.sh doctor
./run.sh open
```

The dashboard opens on the local machine. Client applications should point their OpenAI-compatible base URL to the local `/v1` endpoint. They do not need the upstream provider keys; the proxy reads those only from the local ignored `key.txt`.

## CC Switch Fallback Entry

To keep a direct relay available when the local service is offline, add Switch Local Proxy as a separate Codex provider in CC Switch. Use `http://127.0.0.1:15722/v1/` as the API request URL, select `Responses`, and use the configured forced model. Leave the API Key blank and ensure the generated Codex provider has `requires_openai_auth = false`: the local proxy does not require a client key and injects the real upstream key locally. Keep this entry out of CC Switch automatic failover to avoid two independent failover layers; use it as a manual switch target.

## Claude Code

Claude Code can use the local Anthropic Messages adapter without receiving an upstream provider key:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:15722
export ANTHROPIC_AUTH_TOKEN=local-proxy
claude
```

`local-proxy` is only a non-secret local placeholder. Switch Local Proxy removes the client credential and injects the selected provider key when forwarding the request. Run `/status` inside Claude Code to verify that the Anthropic base URL points to the local service.

To persist the configuration, add the same values to the `env` object in `~/.claude/settings.json`. Do not put real upstream credentials in a project-level Claude settings file.

Supported Anthropic paths:

- `POST /v1/messages`
- `POST /v1/messages/count_tokens`

The adapter preserves Anthropic headers such as `anthropic-version` and `anthropic-beta`, replaces the model with `forced_model`, and supports Anthropic streaming SSE events.

## Import Existing Keys

To import provider lines from another local file:

```bash
./run.sh import-keys /absolute/path/to/source/key.txt
```

The importer copies provider key lines and the optional `upstream_url` and `forced_model` settings without printing key values.

## Configuration

Each non-setting line in `key.txt` uses this format:

```text
provider-name:provider-api-key
```

The optional settings are:

| Setting | Meaning |
| --- | --- |
| `upstream_url` | Base URL for the selected adapter, including `/v1` when required |
| `forced_model` | Model sent to the upstream service |

The dashboard persists non-secret routing state in the ignored `runtime/` directory. Key values and private upstream addresses stay in `key.txt` and are never returned by the status API.

## Failover Policy

Providers are tried in priority order. An HTTP 502 is retried once on the same provider after a one-second delay. A second 502, 429, other 5xx responses, network errors, timeouts, or early SSE failures record one failure and move to the next eligible provider. A provider is cooled down temporarily, then probed again after the cooldown expires.

The proxy does not retry after productive streaming output has already reached the client, because replaying that request could duplicate or truncate a response.

## Commands

| Command | Purpose |
| --- | --- |
| `./run.sh verify` | Run tests, syntax checks, locale validation, and build `dist/` |
| `./run.sh privacy-check` | Scan the working tree, Git history, and `dist/` for private data |
| `./run.sh install` | Install or upgrade the background LaunchAgent |
| `./run.sh doctor` | Check configuration, permissions, service identity, and health |
| `./run.sh repair` | Rebuild and reinstall with rollback on failed health checks |
| `./run.sh start` / `stop` | Start or stop the local service |
| `./run.sh status` | Show process and health information |
| `./run.sh migrate` | Update the supported Codex provider base URL with a local backup |

## Compatibility

This release implements pass-through adapters for OpenAI Responses, OpenAI-compatible Chat Completions, and Anthropic Messages. Grok can use its OpenAI-compatible Responses endpoint. Gemini, Qwen, DeepSeek, Mistral, and similar services can use Chat Completions when their upstream exposes that compatibility endpoint. Native Gemini `generateContent` is not implemented. The configured upstream must support the wire protocol used by the client; this proxy does not translate request or response formats, and one local configuration still uses one upstream base URL and forced model for all paths.

Claude Code gateway configuration follows the [official Claude Code LLM gateway guide](https://code.claude.com/docs/en/llm-gateway-connect). Anthropic request paths and headers follow the [Messages API reference](https://platform.claude.com/docs/en/api/messages/create).

## Security

The service listens on loopback by default and rejects browser requests whose `Origin` is not local. Do not expose it to a network without adding authentication and access controls. Do not put real keys, private upstream URLs, or client credentials in source files, documentation, screenshots, logs, or issue reports. Run `./run.sh privacy-check` before every public push.

## License

This project is released under The Unlicense. See [LICENSE](LICENSE).
