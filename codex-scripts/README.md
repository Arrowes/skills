# Codex 本机脚本

## Codex Proxy Guard

`CodexProxyGuard.ps1` 同步 Windows 的本机代理设置到 Codex 环境，并检查 ChatGPT 连通性。连续失败时，它通过 Clash Verge Rev 的本地控制接口选择可用的非香港节点；不调用模型。

常用命令：

```powershell
# 单次同步
.\CodexProxyGuard.ps1 -Mode Once

# 查看状态或测试健康度
.\CodexProxyGuard.ps1 -Mode Status
.\CodexProxyGuard.ps1 -Mode Health

# 管理员 PowerShell 中安装后台任务
.\CodexProxyGuard.ps1 -Mode Install
```

安装后的任务名为 `Codex Proxy Guard`，使用 `S4U + Limited` 和隐藏 PowerShell 参数。运行日志写入 `~/.codex/proxy-guard/`，不会污染 Git 工作区。脚本只接受回环地址代理，并从当前用户的 Clash Verge Rev 配置读取本地 API secret；不要把该配置或 secret 提交到仓库。
