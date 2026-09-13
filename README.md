# Skills

个人 Agent Skill 收藏与开发仓库。

## 飞书 CLI、Codex Bridge 与自动提醒

这部分记录当前 Windows 机器上的飞书接入方案，目标是在全新机器上把本节直接交给 Codex，即可重建相同能力。仓库不保存 App Secret、访问令牌、用户 ID 或会话 ID。

### 组件

| 组件 | 当前版本（2026-09-13） | 来源 | 作用 |
| --- | --- | --- | --- |
| `@larksuite/cli`（命令 `lark-cli`） | `1.0.89` | [larksuite/cli](https://github.com/larksuite/cli) | 飞书/Lark 官方 CLI。配置应用身份、授权用户身份，并调用消息、文档、日历等开放平台接口。 |
| `lark-channel-bridge` | `0.7.0` | [zarazhangrui/feishu-claude-code-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge) | 社区桥接工具。把飞书私聊或群聊消息转给本地 Codex/Claude CLI，并把结果发回飞书。 |

新机器优先安装最新版；上表版本只用于复现和排障，不作为版本锁定要求。bridge 不是飞书官方项目。

### 安装与基本使用

前置条件：Node.js `>= 20.12.0`、已安装并登录 Codex CLI，以及一个飞书/Lark PersonalAgent 应用。

```powershell
# 安装飞书官方 CLI
npx @larksuite/cli@latest install

# 验证；只有需要访问个人日历、云文档等资源时才需要用户授权
lark-cli --version
lark-cli config init
lark-cli auth login --recommend
lark-cli auth status

# 用机器人身份发消息
lark-cli im +messages-send --as bot --chat-id "oc_xxx" --text "Hello"

# 安装并首次运行 Codex bridge；按终端提示扫码创建或绑定 PersonalAgent
npm i -g lark-channel-bridge
lark-channel-bridge run --profile codex --agent codex

# 验证后注册为 Windows 后台服务
lark-channel-bridge start --profile codex --agent codex
lark-channel-bridge status --profile codex
lark-channel-bridge restart --profile codex
```

bridge 在 Windows 上使用计划任务维持后台服务。每个 profile 的配置、身份和日志位于 `~/.lark-channel/profiles/<profile>/`。在 bridge 启动的进程中调用 `lark-cli` 时，应保留 bridge 注入的 `LARK_CHANNEL`、`LARK_CHANNEL_HOME`、`LARK_CHANNEL_PROFILE`、`LARK_CHANNEL_CONFIG` 和 `LARKSUITE_CLI_CONFIG_DIR`，不要绕回普通本机配置。

### 不消耗模型 Token 的实现原则

定时任务直接运行 PowerShell 和 `lark-cli`，不向 Codex 发送自然语言请求：

1. 额度查询通过 `codex app-server --stdio` 的 `account/rateLimits/read` 读取状态，不创建模型对话。
2. 消息通过 `lark-cli im +messages-send --as bot` 直接发到飞书私聊。
3. 关机卡片按钮只携带固定格式的本地 `cmd`，bridge 记录按钮事件后不转发给 Agent。
4. 本地监听脚本只接受当天、指定私聊和白名单命令，然后调用固定系统命令。

需要零模型 Token 时，按钮不能使用 `__bridge_cb: true`；该字段会让 bridge 把点击事件交给 Codex，产生一次模型调用。飞书开放平台请求、bridge 进程和本地脚本仍会产生少量网络、CPU 与日志开销。

### Codex 额度提醒

当前脚本：`~/.codex/quota-notifier/Send-CodexQuota.ps1`。

| 项目 | 当前行为 |
| --- | --- |
| 定时推送 | 每天 `00:00`，以及活动时段的 `10:00、12:00、14:00、16:00、18:00、20:00、22:00` 私聊推送。午休后的 `13:00` 延后到 `14:00`。 |
| 低额度监控 | `10:05–23:50` 每 15 分钟检查；5 小时或 7 天额度首次低于 20% 时提醒一次，避免重复轰炸。 |
| 数据来源 | `account/rateLimits/read`，读取 5 小时额度、7 天额度、重置次数、最近到期时间和额外额度。 |
| 计划任务 | `CodexQuotaNotifier`、`CodexQuotaLowAlert`。 |

卡片使用 CardKit 2.0：

- 标题显示总体状态与查询时间，不显示套餐信息。
- 第一块显示 5 小时额度，第二块显示 7 天额度；上下排列并使用相同标签宽度。
- 每块第一行依次为名称、剩余百分比、具体重置日期；第二行依次为进度条、距离重置的剩余时间。
- 两块颜色独立变化：剩余 `<20%` 为红色，`20%–49%` 为橙色，`>=50%` 为绿色。
- 下方显示可用重置次数和最近到期时间。到期时间大于 2 天使用普通颜色，剩余不超过 2 天为橙色，不超过 1 天为红色；没有次数时显示“无”。
- 只有确实存在额外额度或无限额度时，才追加“额外额度”一行。

### 关机提醒

当前脚本：

- `~/.lark-channel/send-shutdown-reminder.ps1`
- `~/.lark-channel/handle-shutdown-card-actions.ps1`

当前行为：

1. 每天 `00:30、01:00、01:30` 向指定用户私聊发送紧凑 CardKit 2.0 卡片。
2. 卡片正文只保留一句状态说明，底部横排“今晚不关”和“立即关机”两个按钮。
3. “立即关机”带二次确认，确认后本地脚本发送简短回执并在 5 秒后关机。
4. “今晚不关”会写入当天状态、取消已经安排的关机，并停止当晚后续提醒。
5. 如果 `01:30` 仍未回应，且提醒卡发送成功、bridge 正常运行，则安排 `01:45` 自动关机；15 分钟内选择“今晚不关”仍可取消。
6. bridge 未运行时不安排自动关机，避免用户无法操作按钮而误关机。

计划任务：

- `Codex-Lark-Shutdown-Reminder-0030`：触发三次提醒。
- `Codex-Lark-Shutdown-Card-Actions`：每天 `00:29` 启动本地监听，每 15 秒检查一次按钮事件，到 `01:46` 结束；监听期间不调用模型。

按钮命令必须带当天日期，例如 `shutdown.skip.20260913` 和 `shutdown.now.20260913`。监听器还要校验目标私聊 ID、去重事件并忽略旧日期，防止旧卡片或重复点击执行动作。

### 给新机器上 Codex 的重建指令

复制下面内容并发给 Codex：

```text
请按照本 README 的“飞书 CLI、Codex Bridge 与自动提醒”章节，在当前 Windows 机器上重建配置。

要求：
1. 安装最新版 @larksuite/cli、lark-channel-bridge，并使用 codex profile。
2. 先检查 Node.js、Codex CLI、现有飞书应用和计划任务；不要覆盖可用配置。
3. 通过当前 bridge/profile 获取实际用户与私聊标识，不要沿用旧机器的 ID。
4. 创建额度提醒和关机提醒 PowerShell 脚本，并注册本章所列 Windows 计划任务。
5. 所有定时查询和消息发送必须绕过模型会话；关机按钮使用本地白名单 cmd，不使用 __bridge_cb。
6. 不把 App Secret、token、用户 ID、chat ID 写入 Git 仓库或输出到对话。
7. 先执行 PowerShell 语法检查、额度查询测试、禁用按钮的样例卡测试和关机动作 dry-run；测试期间不得真正关机。
8. 最后核对任务触发时间、bridge 状态、私聊收件人和日志，并报告脚本路径及验证结果。
```

迁移时还要在新机器上重新完成 PersonalAgent 绑定；Git 仓库只保存技术方案，不保存身份凭据。

## 自定义 Skill

### interview-review-coach

面向计算机视觉、自动驾驶感知、BEV、模型部署和算法工程岗位的面试复盘助手。它可以把面试录音、转录或笔记整理为真实回答记录、改进后的建议回答、暴露的知识缺口和下一轮复习优先级，并附带本地 Whisper 转录脚本。

目录：[`interview-review-coach/`](./interview-review-coach/)

## Skill 分类

### Codex 系统能力

| Skill | 来源 | 作用 |
| --- | --- | --- |
| `imagegen` | [openai/skills](https://github.com/openai/skills) | 生成和编辑照片、插画、纹理及透明背景位图。 |
| `openai-docs` | [openai/skills](https://github.com/openai/skills) | 查询 Codex、ChatGPT、OpenAI API、模型和配置的官方资料。 |
| `plugin-creator` | [openai/skills](https://github.com/openai/skills) | 创建 Codex 插件、MCP 配置和个人市场条目。 |
| `review-agent` | [openai/skills](https://github.com/openai/skills) | 对代码变更执行只读、缺陷优先的审查。 |
| `skill-creator` | [openai/skills](https://github.com/openai/skills) | 设计、创建、修改和验证 Agent Skill。 |
| `skill-installer` | [openai/skills](https://github.com/openai/skills) | 从官方目录或 GitHub 仓库安装 Skill。 |

### 搜索与技能发现

| Skill | 来源 | 作用 |
| --- | --- | --- |
| `anysearch` | [anysearch-ai/anysearch-skill](https://github.com/anysearch-ai/anysearch-skill) | 实时网页搜索、垂直搜索、批量搜索和网页内容提取。 |
| `find-skills` | [vercel-labs/skills](https://github.com/vercel-labs/skills) | 搜索并安装开放 Agent Skill。 |

### 软件开发

| Skill | 来源 | 作用 |
| --- | --- | --- |
| `ponytail` | [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) | 强制采用简单、短小、低依赖且真正可行的编码方案。 |
| `superpowers` | [obra/superpowers](https://github.com/obra/superpowers) | 提供需求澄清、计划、TDD、调试、代理协作、审查和验证等完整开发方法论。 |

### 求职与面试

| Skill | 来源 | 作用 |
| --- | --- | --- |
| `interview-review-coach` | [本仓库](./interview-review-coach/) | 复盘技术面试，整理真实回答、建议回答、知识缺口和学习计划。 |
| `job-hunter` | [Donzhu2020/job-tracker](https://github.com/Donzhu2020/job-tracker) | 搜索职位、匹配简历、生成求职信并将结果保存到 Obsidian。 |

### 视觉与视频创作

| Skill | 来源 | 作用 |
| --- | --- | --- |
| `cinematic-director-frame` | [zhu930824/cinematic-director-frame](https://github.com/zhu930824/cinematic-director-frame) | 生成具有导演风格、镜头语言和宽银幕构图的电影画面。 |
| `chatcut` | [ChatCut-Inc/agent-plugin](https://github.com/ChatCut-Inc/agent-plugin) | 在 Codex 中完成素材导入、时间线剪辑、字幕、配音、生成和导出。 |
