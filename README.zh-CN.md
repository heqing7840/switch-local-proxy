# Switch Local Proxy

[English](README.md)

一个本地 AI API 代理，提供多 Key 优先级路由、自动故障转移、重试、冷却和 Provider 状态监控。

当前版本接收 OpenAI Responses、OpenAI 兼容 Chat Completions 和 Anthropic Messages 请求，所有适配器共用 Provider 优先级、健康状态、重试、冷却和监控逻辑。

## 功能

- 按优先级在多个 API Key 之间路由。
- HTTP 502 在同一线路等待 1 秒重试一次，再决定是否切换。
- 临时冷却和到期自动恢复探测。
- 向客户端转发 SSE 前识别早期错误事件。
- 支持 OpenAI Responses、Chat Completions 和 Anthropic Messages 透传适配。
- 管理页显示线路状态、延迟、失败原因、脱敏 Key、模型路由和请求记录。
- 支持在管理页添加、编辑、删除、启停、排序和恢复 Provider。
- 不依赖数据库或云端控制台，运行状态保存在本机。
- 默认只监听回环地址，通过 macOS 后台 LaunchAgent 常驻运行。
- `RunAtLoad` 在登录时自动启动，`KeepAlive` 在异常退出后自动拉起；`./run.sh doctor` 会校验两项设置。
- 独立的无窗口 Python 健康守护每 15 秒检查一次；主任务被卸载、停止或健康接口无响应时，会重新加载并启动服务。
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

首次启动会在被忽略的 `runtime/` 目录中创建本机 SQLite 数据库。密钥只保存在该数据库中，不会提交到 GitHub。

然后运行：

```bash
./run.sh verify
./run.sh install
./run.sh doctor
./run.sh open
```

即使还没有添加渠道，`install` 与 `doctor` 也可以通过。打开管理页后，请先添加至少一条渠道，再让客户端连接本地 `/v1` 接口；客户端不需要知道上游密钥，代理只从本机 SQLite 读取。

如果之后移动了项目目录，请重新执行 `./run.sh repair`，让 LaunchAgent 绑定到新路径。

## 更新检查

管理页打开后会在后台检查项目公开的版本文件。成功结果缓存 24 小时；临时网络失败后 15 分钟再次尝试。检查只请求公开的 `version.json`，不会上传 API Key、渠道名称、请求记录、私有上游地址或设备标识；检查失败也绝不会影响代理转发。

发现新版本时，页头会链接到公开提交记录。升级命令：

```bash
git pull --ff-only
./run.sh repair
```

## CC Switch 后备条目

为了在本地服务离线时仍能快速切回直连线路，可在 CC Switch 中把 Switch Local Proxy 添加为独立 Codex Provider。API 请求地址填写 `http://127.0.0.1:15722/v1/`，上游格式选择 `Responses`。API Key 保持空白，并确保生成的 Codex Provider 配置为 `requires_openai_auth = false`：本地代理不需要客户端密钥，会在本机注入真实上游 Key。不要把该条目加入 CC Switch 自动故障转移队列，避免两层故障转移叠加；将它作为手动切换目标使用。

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

适配器会保留 `anthropic-version`、`anthropic-beta` 等 Anthropic 请求头，默认原样转发客户端请求的模型名，并识别 Anthropic 流式 SSE 事件。

## 配置格式

每条渠道在管理页中配置，并保存到本机 SQLite。添加渠道只需填写名称、API 地址和 API Key；协议默认自动匹配，模型由客户端请求决定。

编辑渠道时会通过仅限本机的管理接口回显已保存的 API 地址；浏览器始终无法读取完整 API Key。

| 配置项 | 含义 |
| --- | --- |
| `upstream_url` | 该渠道独立的 API 前缀 |
| `protocol` | `auto`、`openai_responses`、`openai_chat_completions` 或 `anthropic_messages` |

管理页把密钥、渠道配置、运行状态和请求记录保存到被忽略的 `runtime/proxy.sqlite3`；完整密钥和私有 API 地址不会由状态接口返回。

## 故障转移策略

代理按优先级请求 Provider，并用内存占位避免并发任务同时挤入同一渠道。HTTP 502 会等待 1 秒并在同一 Provider 重试一次；429、账户并发限制和明确的上游繁忙会原样返回 Codex，代理不在当前请求内重试且不增加失败计数。同一 Provider 在 60 秒内连续出现 2 次此类错误时短暂避让 10 秒，让 Codex 的下一次重试选择下一个 Key，随后自动恢复。其他明确线路故障才记录失败并进入常规熔断。

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

当前版本支持以下渠道类型：

- GPT/Codex 与 Grok：通过 OpenAI Responses 接口接入。
- Gemini、通义千问、DeepSeek、Mistral 及其它 OpenAI 兼容服务：通过 Chat Completions 接口接入。
- Claude：通过 Anthropic Messages 接口接入。

每条渠道都有独立 API 地址、API Key 和协议设置，模型名默认由 Codex、Claude Code 或其它客户端随请求传入，不需要在渠道表单中配置。原生 Gemini `generateContent` 尚未实现；上游必须提供上述某一种兼容协议，代理不在不同协议之间转换请求或响应格式。

内置 GPT 防护会把 `gpt-*` 请求（包括偶发的 `gpt-5.6-luna`）改写为本机全局 GPT 模型，避免不支持 Luna 的中继报错。支持 Luna 的渠道可在编辑页把“GPT 模型策略”改为“使用客户端请求模型”；其它模型家族不受该防护影响。界面不允许手工输入具体模型名。

## 来源访问控制

服务监听本机网卡，但管理页和管理 API 始终仅限本机。模型代理请求采用两级来源规则：全局默认“仅限本机”，单个渠道默认“跟随全局”；渠道设置为其它规则时以渠道设置优先。支持仅本机、仅局域网、所有来源，以及一行一条 IPv4、IPv6 或 CIDR 的指定范围。管理页会显示当前可连接的本机 IPv4 地址。

Claude Code 配置依据 [Claude Code 官方 LLM Gateway 指南](https://code.claude.com/docs/en/llm-gateway-connect)，请求路径和请求头依据 [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create)。

## 安全

服务默认仅允许本机来源使用渠道；只有用户明确选择局域网、指定范围或所有来源时才允许远程代理请求。管理接口始终仅限本机，并继续拒绝不匹配的浏览器 `Origin`。选择“所有来源”会让任何能连接端口的设备消耗该渠道额度，应谨慎使用。不要把真实 Key、私有上游地址或客户端凭据写入源码、文档、截图、日志或 Issue。每次公开推送前都应运行 `./run.sh privacy-check`。

## 许可证

本项目使用 The Unlicense 发布，详见 [LICENSE](LICENSE)。
