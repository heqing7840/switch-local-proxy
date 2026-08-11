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

The first launch creates a local SQLite database under the ignored `runtime/` directory. Configure channels from the dashboard; credentials remain in that database and are never committed.

Then run:

```bash
./run.sh verify
./run.sh install
./run.sh doctor
./run.sh open
```

The dashboard opens on the local machine. Client applications should point their OpenAI-compatible base URL to the local `/v1` endpoint. They do not need upstream provider credentials; the proxy reads them only from the local SQLite database.

## Updates

The dashboard checks the project's public version manifest in the background when it is open. A successful result is cached for 24 hours; a temporary network failure retries after 15 minutes. It sends only a generic request for the public `version.json` file: no API keys, provider names, request history, private upstream address, or device identifier leaves the machine. An unavailable update check never affects proxy traffic.

When a newer version is available, the header links to the public commit history. Upgrade with:

```bash
git pull --ff-only
./run.sh repair
```

## CC Switch Fallback Entry

To keep a direct relay available when the local service is offline, add Switch Local Proxy as a separate Codex provider in CC Switch. Use `http://127.0.0.1:15722/v1/` as the API request URL and select `Responses`. Leave the API Key blank and ensure the generated Codex provider has `requires_openai_auth = false`: the local proxy does not require a client key and injects the real upstream key locally. Keep this entry out of CC Switch automatic failover to avoid two independent failover layers; use it as a manual switch target.

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

The adapter preserves Anthropic headers such as `anthropic-version` and `anthropic-beta`, forwards the client-requested model by default, and supports Anthropic streaming SSE events.

## Configuration

Each channel is configured in the dashboard and stored in SQLite. Adding a channel requires only a name, API URL, and API key. Protocol matching defaults to automatic, and the model comes from the client request.

| Setting | Meaning |
| --- | --- |
| `upstream_url` | Channel-specific API prefix |
| `protocol` | `auto`, `openai_responses`, `openai_chat_completions`, or `anthropic_messages` |

The dashboard persists credentials, routing state, runtime health, and request metadata in the ignored `runtime/proxy.sqlite3` database. Full credentials and private API addresses are never returned by the status API.

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

This release supports these channel types:

- GPT/Codex and Grok through OpenAI Responses.
- Gemini, Qwen, DeepSeek, Mistral, and other OpenAI-compatible services through Chat Completions.
- Claude through Anthropic Messages.

Every channel has its own API URL, API key, and protocol setting. The model name is supplied by Codex, Claude Code, or another client, so it does not need to be configured in the channel form. Native Gemini `generateContent` is not implemented; the upstream must expose one of the supported compatible protocols, and the proxy does not translate between request or response formats.

The built-in GPT guard rewrites `gpt-*` requests, including occasional `gpt-5.6-luna` requests, to the local global GPT model so relays without Luna support do not fail. A Luna-capable channel can select **Use client-requested model** in its GPT model policy. Other model families are unaffected, and the UI never accepts a manually typed model name.

## Source Access Control

The service listens on local network interfaces, while the dashboard and management APIs remain loopback-only. Proxy requests use two access levels: the global policy defaults to local-only, and channels follow it unless they define an override. Policies support local-only, private LAN, all sources, or one IPv4, IPv6, or CIDR rule per line. The dashboard lists the current machine's usable IPv4 proxy addresses.

Claude Code gateway configuration follows the [official Claude Code LLM gateway guide](https://code.claude.com/docs/en/llm-gateway-connect). Anthropic request paths and headers follow the [Messages API reference](https://platform.claude.com/docs/en/api/messages/create).

## Security

Channel access defaults to local-only. Remote proxy requests are accepted only after a user explicitly selects LAN, a bounded IP range, or all sources; management endpoints always remain loopback-only and retain browser Origin checks. The **All sources** option allows any device that can reach the port to consume that channel's quota and should be used cautiously. Do not put real keys, private upstream URLs, or client credentials in source files, documentation, screenshots, logs, or issue reports. Run `./run.sh privacy-check` before every public push.

## License

This project is released under The Unlicense. See [LICENSE](LICENSE).
