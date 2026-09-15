# 飞书与 Codex 自动提醒

这一目录把额度提醒、低额度监控、卡片按钮和关机提醒集中到一个后台协调任务中，适合在新的 Windows 机器上复用。

## 组件

| 组件 | 官方或项目链接 | 作用 |
| --- | --- | --- |
| `@larksuite/cli` / `lark-cli` | [larksuite/cli](https://github.com/larksuite/cli) | 飞书官方 CLI，用机器人身份发送卡片和更新消息。 |
| `lark-channel-bridge` | [zarazhangrui/lark-coding-agent-bridge](https://github.com/zarazhangrui/lark-coding-agent-bridge) | 社区桥接工具，把飞书消息交给本地 Codex，并记录卡片命令。 |
| `LarkAutomation` | 本目录 | 单一 Windows 后台任务，负责调度提醒和处理按钮。 |

## 文件

| 文件 | 作用 |
| --- | --- |
| `Lark-Automation.ps1` | 唯一常驻协调器：等待 bridge、执行时间表、扫描一次卡片事件日志。 |
| `Quota.ps1` | 按需读取 Codex 额度并生成或发送额度卡。 |
| `Shutdown-Reminder.ps1` | 按需生成并发送关机卡。 |
| `Shutdown-Actions.ps1` | 执行白名单关机操作并更新原卡按钮。 |
| `Install.ps1` | 创建本机配置并注册 `S4U + Limited` 的隐藏任务。 |

配置、状态、日志和任务备份保存在 `~/.lark-channel/automation/`，不会写入 Git。

## 当前行为

额度卡在每天 `00:00、10:00、12:00、14:00、16:00、18:00、20:00、22:00` 发送。电脑开机后，协调器检测到 `codex` profile 的 bridge 成功上线时也会发送一次，并按 Windows 启动时间去重。`10:05–23:50` 每 15 分钟检查额度，5 小时或 7 天额度首次低于 20% 时提醒。

额度卡显示两行等宽布局、独立颜色和进度条。下方显示重置次数及最近到期时间；只有确实存在额外额度时才追加该行。“查询额度”按钮会重新发送最新额度卡。

关机卡在每天 `00:30、01:00、01:30` 发送。“今晚不关”取消当天后续提醒及已安排的关机；若 01:30 仍未回应，则在 bridge 可用且卡片发送成功时安排 01:45 自动关机。“立即关机”带二次确认，历史卡仍可使用；点击后原按钮显示“正在关机…”，5 秒后恢复，再执行强制关机。

## Token 与安全边界

定时和按钮路径不创建模型会话：额度通过 `codex app-server --stdio` 的 `account/rateLimits/read` 获取，飞书消息由 `lark-cli` 发送，按钮只写入固定 `cmd` 并由本地脚本处理。卡片不要添加 `__bridge_cb: true`，否则点击会转给 Agent 并产生模型调用。

脚本只接受配置中指定私聊的 `quota.refresh`、`shutdown.skip.YYYYMMDD` 和 `shutdown.now.YYYYMMDD`。旧日期的 `skip` 无效；旧卡中的 `now` 仍可远程关机。不要把真实 ID、App Secret 或 token 提交到仓库。

## 新机器安装

前置条件：Node.js、已登录的 Codex CLI、PowerShell 7、飞书 PersonalAgent，以及已配置的 `lark-cli` 和 `lark-channel-bridge`。

```powershell
npx @larksuite/cli@latest install
npm i -g lark-channel-bridge
lark-cli --version
lark-channel-bridge run --profile codex --agent codex
```

确认 bridge 可用后，在管理员 PowerShell 中运行：

```powershell
.\Install.ps1 -RecipientOpenId "ou_xxx" -RecipientChatId "oc_xxx"
```

安装器会自动寻找 Node、Codex、PowerShell 7 和 `lark-cli`，生成仓库外配置，先做不发消息且不关机的验证，再注册一个无窗口任务。若旧版五任务存在，安装器先备份，确认新任务运行后再停用它们。

## 验证与回退

```powershell
Get-ScheduledTask -TaskName LarkAutomation |
  Select-Object TaskName, State, @{n='LogonType';e={$_.Principal.LogonType}}, @{n='RunLevel';e={$_.Principal.RunLevel}}

Get-Content "$HOME\.lark-channel\automation\automation.log" -Tail 30
```

正常结果是 `Running / S4U / Limited`。如需回退，先停用 `LarkAutomation`，再从 `~/.lark-channel/automation/task-backups-*` 导入旧任务定义。

## 给新机器 Codex 的指令

```text
请阅读 lark-scripts/README.md，先检查现有 Node、Codex、lark-cli、lark-channel-bridge 和计划任务，再使用 Install.ps1 重建自动提醒。实际用户 ID 和私聊 ID 必须从新机器当前 profile 获取，不得沿用旧机器值。配置和凭据不得写入 Git。验证时不得发送样例卡或执行真实关机；最后核对 LarkAutomation 为 Running、S4U、Limited，并确认旧提醒任务已备份和停用。
```
