# Switch Local Proxy

[English](README.md)

一个本地 AI API 代理，提供多 Key 优先级路由、自动故障转移、重试、冷却和 Provider 状态监控。

当前版本接收 OpenAI Responses 和 Anthropic Messages 兼容请求，两种适配器共用 Provider 优先级、健康状态、重试、冷却和监控逻辑。

## 功能

- 按优先级在多个 API Key 之间路由。
- HTTP 502 在同一线路等待 1 秒重试一次，再决定是否切换。
- 临时冷却和到期自动恢复探测。
- 向客户端转发 SSE 前识别早期错误事件。
- 支持 OpenAI Responses 和 Anthropic Messages 透传适配。
- 管理页显示线路状态、延迟、失败原因、脱敏 Key、模型路由和请求记录。
- 支持在管理页添加、编辑、删除、启停、排序和恢复 Provider。
- 不依赖数据库或云端控制台，运行状态保存在本机。
- 默认只监听回环地址，通过 macOS 后台 LaunchAgent 常驻运行。
- 拒绝跨站网页写请求、限制请求体大小，并且不会把客户端 Cookie 转发给上游。
- 发布门禁会检查当前文件、未忽略的新文件、全部 Git 历史和 `dist/` 白名单，且不会打印命中的敏感值。

## 环境要求

- macOS，支持 `launchctl`。
- Python 3.11 或更高版本。
- 与当前适配器兼容的上游 API 地址。

## 快速开始

```bash
git clone https://github.com/heqing7840/switch-local-proxy.git
cd switch-local-proxy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

创建被 Git 忽略的本机 `key.txt`，不要提交该文件：

```text
upstream_url:<your-upstream-api-base>/v1
forced_model:<your-model-name>
provider-one:<your-api-key>
provider-two:<your-api-key>
```

然后运行：

```bash
chmod 600 key.txt
./run.sh verify
./run.sh install
./run.sh doctor
./run.sh open
```

管理页会在本机打开。客户端只需要连接本地 `/v1` 接口，不需要知道上游 Key；代理只从本机被忽略的 `key.txt` 读取它们。

## Claude Code

Claude Code 可以通过本地 Anthropic Messages 适配器使用代理，同时不接触真实的上游 Provider Key：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:15722
export ANTHROPIC_AUTH_TOKEN=local-proxy
claude
```

`local-proxy` 只是一个不敏感的本地占位值。Switch Local Proxy 会移除客户端凭据，并在转发时注入当前选中 Provider 的真实 Key。在 Claude Code 中运行 `/status`，可以确认 Anthropic Base URL 已指向本地服务。

如需长期生效，可把相同变量写入 `~/.claude/settings.json` 的 `env` 对象。不要把真实上游密钥写进项目级 Claude 配置。

当前支持的 Anthropic 路径：

- `POST /v1/messages`
- `POST /v1/messages/count_tokens`

适配器会保留 `anthropic-version`、`anthropic-beta` 等 Anthropic 请求头，将模型替换为 `forced_model`，并识别 Anthropic 流式 SSE 事件。

## 导入已有 Key

可以从另一个本地文件导入 Provider：

```bash
./run.sh import-keys /absolute/path/to/source/key.txt
```

导入器会复制 Provider Key 行，以及可选的 `upstream_url` 和 `forced_model`，不会输出 Key 内容。

## 配置格式

每条 Provider 使用以下格式：

```text
provider-name:provider-api-key
```

可选配置项：

| 配置项 | 含义 |
| --- | --- |
| `upstream_url` | 当前适配器使用的上游基地址，需要 `/v1` 时一并写入 |
| `forced_model` | 实际发送给上游的模型名 |

管理页把非敏感路由状态保存到被忽略的 `runtime/`；Key 和私有上游地址只保存在 `key.txt`，不会由状态接口返回。

## 故障转移策略

代理按优先级请求 Provider。HTTP 502 会等待 1 秒并在同一 Provider 重试一次；第二次 502、429、其他 5xx、网络错误、超时或早期 SSE 失败会记录一次失败并切换到下一条可用线路。冷却时间结束后，下一次请求会自动重新探测。

已经向客户端输出有效流内容后不再重试，避免重复生成或截断结果。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `./run.sh verify` | 测试、语法检查、语言资源校验并构建 `dist/` |
| `./run.sh privacy-check` | 扫描工作区、Git 历史和 `dist/` 中的隐私信息 |
| `./run.sh install` | 安装或升级后台 LaunchAgent |
| `./run.sh doctor` | 检查配置、权限、服务身份和健康状态 |
| `./run.sh repair` | 构建并重新安装，健康检查失败时自动回滚 |
| `./run.sh start` / `stop` | 启动或停止本地服务 |
| `./run.sh status` | 查看进程和健康信息 |
| `./run.sh migrate` | 备份并更新受支持的 Codex Provider 地址 |

## 兼容范围

当前版本实现 OpenAI Responses 与 Anthropic Messages 两种透传适配器。上游服务必须支持客户端使用的协议；如果上游只支持 OpenAI Responses，Claude Code 不能仅靠本代理直接使用，仍需要额外的请求与响应格式转换层。Gemini 等其他协议仍需增加对应适配器。

Claude Code 配置依据 [Claude Code 官方 LLM Gateway 指南](https://code.claude.com/docs/en/llm-gateway-connect)，请求路径和请求头依据 [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create)。

## 安全

服务默认只监听回环地址，并拒绝 `Origin` 不是本机的浏览器请求。除非另行增加认证和访问控制，否则不要把它暴露到局域网或公网。不要把真实 Key、私有上游地址或客户端凭据写入源码、文档、截图、日志或 Issue。每次公开推送前都应运行 `./run.sh privacy-check`。

## 许可证

本项目使用 The Unlicense 发布，详见 [LICENSE](LICENSE)。
