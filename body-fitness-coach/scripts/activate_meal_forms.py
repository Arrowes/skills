"""One-shot activation after the current bridge agent has exited.

Run via Task Scheduler/pythonw, so stopping the bridge cannot kill this verifier.
Only restarts the exact configured bridge PID; personal runtime state is private.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import time

import coach


def powershell(script):
    result = coach.run_process(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
    if result.returncode:
        raise RuntimeError("PowerShell operation failed")
    return result.stdout.strip()


def process(pid):
    raw = powershell(f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    return json.loads(raw) if raw else None


def notify(config, text):
    coach.ensure_lark_channel_env()
    result = coach.run_process(coach.command_prefix(config["lark_cli"]) + ["im", "+messages-send", "--as", "bot", "--chat-id", config["chat_id"], "--text", text], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
    return result.returncode == 0


def activate(agent_pid, bridge_pid):
    status = coach.DATA_DIR / "meal-activation.json"
    def save(state, **extra):
        status.write_text(json.dumps({"state": state, "time": datetime.now().astimezone().isoformat(), **extra}, ensure_ascii=False, indent=2), encoding="utf-8")
    config = coach.load_config()
    save("waiting_for_agent_exit")
    try:
        agent = process(agent_pid)
        if agent and (agent["Name"].lower() != "codex.exe" or agent["ParentProcessId"] != bridge_pid):
            raise RuntimeError("Unexpected agent identity; refusing restart")
        deadline = time.monotonic() + 1200
        while process(agent_pid):
            if time.monotonic() > deadline:
                raise RuntimeError("Current agent still active; no restart performed")
            time.sleep(5)
        # Leave time for the final bridge message to finish sending.
        time.sleep(12)
        while True:
            children = powershell(f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={int(bridge_pid)}' | Where-Object {{ $_.Name -eq 'codex.exe' }} | Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress")
            if not children:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Another bridge agent is active; no restart performed")
            time.sleep(5)
        old = process(bridge_pid)
        if old:
            command = old.get("CommandLine") or ""
            if old["Name"].lower() != "node.exe" or "lark-channel-bridge" not in command or "--profile codex" not in command or "--agent codex" not in command:
                raise RuntimeError("Bridge PID identity changed; refusing restart")
            powershell(f"Stop-Process -Id {int(bridge_pid)} -ErrorAction Stop")
        save("restarting")
        launcher = coach.SKILL_ROOT.parent / "lark-scripts" / "Bridge-Startup.ps1"
        if not launcher.is_file():
            raise RuntimeError("Existing bridge launcher missing")
        # A long-lived descendant can inherit pipe handles and keep communicate()
        # blocked after PowerShell exits. Use a private file for launcher output.
        with (coach.DATA_DIR / "meal-activation-launcher.log").open("a", encoding="utf-8") as log:
            started = coach.run_process(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(launcher)], stdout=log, stderr=log, stdin=coach.subprocess.DEVNULL, close_fds=True, timeout=400)
        if started.returncode:
            raise RuntimeError("Bridge launcher failed; inspect launcher logs")
        raw = powershell("Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | Where-Object { $_.CommandLine -match 'lark-channel-bridge' -and $_.CommandLine -match '--profile codex' } | Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress")
        pids = json.loads(raw) if raw else []
        pids = pids if isinstance(pids, list) else [pids]
        if len(pids) != 1 or pids[0] == bridge_pid:
            raise RuntimeError("Expected exactly one new bridge instance")
        # The fresh process loaded the installed handler. Send a real form only now.
        result = coach.run_process([str(Path(coach.sys.executable).with_name("python.exe")), str(coach.ROOT / "coach.py"), "meal-reminder"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=100)
        if result.returncode:
            raise RuntimeError("Bridge restarted but meal form delivery failed")
        save("active", bridge_pid=pids[0])
        delivered = notify(config, "Body coach 三餐表单已启用，bridge 已完成重启。今天的提醒卡已发送（若三餐均已记录则不再发送）。下拉选择或填写自定义内容后，点击“记录所填餐次”；记录成功后会收到营养回执。")
        save("active", bridge_pid=pids[0], confirmation_delivered=delivered)
    except Exception as exc:
        save("failed", reason=type(exc).__name__ + ": " + str(exc))
        notify(config, "Body coach 三餐表单代码已安装，但自动启用未完成。请发“检查三餐表单启用状态”，我会根据本地日志继续处理；暂不要把提交无回执视为成功。")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-pid", type=int, required=True)
    parser.add_argument("--bridge-pid", type=int, required=True)
    args = parser.parse_args()
    activate(args.agent_pid, args.bridge_pid)
