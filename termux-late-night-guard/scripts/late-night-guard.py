#!/data/data/com.termux/files/usr/bin/python
"""Minimal light-based late-night alarm for Termux on Android."""

from __future__ import annotations

import json
import math
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path


HOME = Path.home()
PREFIX = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
BIN = PREFIX / "bin"
os.environ["PATH"] = f"{BIN}:{os.environ.get('PATH', '')}"

SCRIPT = HOME / "server" / "late-night-guard.py"
STATE_DIR = HOME / ".local" / "state" / "late-night-guard"
STATE_FILE = STATE_DIR / "state.json"
ALARM_FILE = Path("/product/media/audio/alarms/Alarm_Classic.ogg")
if not ALARM_FILE.exists():
    ALARM_FILE = Path("/system/media/audio/alarms/AlarmClock.ogg")

PREP_TIME = clock_time(0, 25)
NORMAL_DEADLINE = clock_time(0, 30)
DELAYED_DEADLINE = clock_time(1, 0)
DELAY_MINUTES = 15
MAX_WEEKLY_DELAYS = 3
CUTOFF_TIME = clock_time(1, 0)
DEFAULT_DARK_MAX = 5.0
PROXIMITY_FAR_MIN = 4.0
UPRIGHT_Y_MIN = 6.5
GUARD_SECONDS = 15 * 60
PREP_NOTIFICATION_ID = "7310"
ALARM_NOTIFICATION_ID = "7311"


def command(*args: str, timeout: int = 10, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BIN / args[0]), *args[1:]],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def restart_termux_api() -> None:
    try:
        command("termux-api-start", timeout=5)
        time.sleep(0.5)
    except (OSError, subprocess.SubprocessError):
        pass


def best_effort(*args: str, timeout: int = 8) -> bool:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            command(*args, timeout=timeout, check=True)
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
            if isinstance(exc, subprocess.TimeoutExpired):
                try:
                    subprocess.run(
                        [str(BIN / "pkill"), "-f", "[t]ermux-api "],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
            if attempt == 0 and args[0] != "termux-api-start":
                restart_termux_api()
                continue
    print(
        f"{datetime.now().isoformat(timespec='seconds')} WARN {args[0]} failed: {type(last_error).__name__}",
        flush=True,
    )
    return False


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def update_state(**changes: object) -> dict:
    state = load_state()
    state.update(changes)
    save_state(state)
    return state


def week_key(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def day_text(day: date) -> str:
    return day.isoformat()


def read_sample(retries: int = 2) -> dict:
    last_error = "unknown sensor error"
    for _ in range(retries + 1):
        try:
            result = command(
                "termux-sensor",
                "-s",
                "LIGHT,PROXIMITY,GRAVITY",
                "-n",
                "1",
                timeout=8,
            )
            if result.returncode != 0:
                last_error = result.stderr.strip() or f"termux-sensor exited {result.returncode}"
                restart_termux_api()
                continue
            raw = json.loads(result.stdout)
            light = float(raw["LIGHT"]["values"][0])
            proximity = float(raw["PROXIMITY"]["values"][0])
            gravity = [float(value) for value in raw["GRAVITY"]["values"][:3]]
            return {
                "ok": True,
                "light": light,
                "proximity": proximity,
                "gravity": gravity,
            }
        except (subprocess.TimeoutExpired, ValueError, KeyError, json.JSONDecodeError, OSError) as exc:
            last_error = str(exc)
            restart_termux_api()
        time.sleep(0.4)
    return {"ok": False, "error": last_error}


def dark_threshold() -> float:
    try:
        return float(load_state().get("dark_max", DEFAULT_DARK_MAX))
    except (TypeError, ValueError):
        return DEFAULT_DARK_MAX


def valid_dark(sample: dict, threshold: float | None = None) -> bool:
    if not sample.get("ok"):
        return False
    threshold = dark_threshold() if threshold is None else threshold
    gravity = sample.get("gravity", [0.0, 0.0, 0.0])
    return (
        sample["light"] <= threshold
        and sample["proximity"] >= PROXIMITY_FAR_MIN
        and len(gravity) >= 2
        and gravity[1] >= UPRIGHT_Y_MIN
    )


def sensor_reason(sample: dict) -> str:
    if not sample.get("ok"):
        return f"传感器异常: {sample.get('error', 'unknown')}"
    reasons: list[str] = []
    if sample["light"] > dark_threshold():
        reasons.append(f"灯亮({sample['light']:.1f} lux)")
    if sample["proximity"] < PROXIMITY_FAR_MIN:
        reasons.append("距离传感器被遮挡")
    gravity = sample.get("gravity", [0.0, 0.0, 0.0])
    if len(gravity) < 2 or gravity[1] < UPRIGHT_Y_MIN:
        reasons.append("手机未竖直摆放")
    return "、".join(reasons) or "有效关灯"


def remove_notification(notification_id: str) -> None:
    try:
        command("termux-notification-remove", notification_id, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def delay_until(day: date, state: dict | None = None) -> datetime | None:
    state = state or load_state()
    if state.get("delay_date") != day_text(day):
        return None
    try:
        value = datetime.fromisoformat(str(state["delay_until"]))
        return min(value, datetime.combine(day, CUTOFF_TIME))
    except (KeyError, TypeError, ValueError):
        return datetime.combine(day, DELAYED_DEADLINE)


def weekly_delay_count(day: date, state: dict | None = None) -> int:
    state = state or load_state()
    if state.get("delay_week") != week_key(day):
        return 0
    try:
        return max(0, int(state.get("delay_count", 1)))
    except (TypeError, ValueError):
        return 1


def add_action_buttons(args: list[str], day: date, state: dict | None = None) -> None:
    state = state or load_state()
    actions = []
    remaining = MAX_WEEKLY_DELAYS - weekly_delay_count(day, state)
    if remaining > 0:
        actions.append(("延迟15分", "delay"))
    if state.get("cancel_week") != week_key(day):
        actions.append(("取消今晚", "cancel"))
    for index, (label, mode) in enumerate(actions, 1):
        args += [
            f"--button{index}",
            label,
            f"--button{index}-action",
            f"{BIN / 'python'} {SCRIPT} {mode}",
        ]


def quota_text(day: date, state: dict | None = None) -> str:
    state = state or load_state()
    remaining = max(0, MAX_WEEKLY_DELAYS - weekly_delay_count(day, state))
    cancel = "可取消" if state.get("cancel_week") != week_key(day) else "取消已用"
    return f"延迟剩{remaining}次，{cancel}"


def notify_prepare(delayed: bool = False) -> None:
    day = datetime.now().date()
    state = load_state()
    args = [
        "termux-notification",
        "--id",
        PREP_NOTIFICATION_ID,
        "--ongoing",
        "--priority",
        "high",
        "--title",
        "熬夜检测仪",
    ]
    if delayed:
        until = delay_until(day, state)
        label = until.strftime("%H:%M") if until else "稍后"
        args += ["--content", f"已延迟至{label}；{quota_text(day, state)}；01:00停止"]
    else:
        args += ["--content", f"00:30检测；{quota_text(day, state)}；01:00停止"]
    add_action_buttons(args, day, state)
    best_effort(*args, timeout=8)


def save_volumes() -> None:
    state = load_state()
    if "saved_volumes" not in state:
        try:
            current = json.loads(command("termux-volume", timeout=8, check=True).stdout)
            state["saved_volumes"] = {
                item["stream"]: int(item["volume"])
                for item in current
                if item["stream"] in {"alarm", "music", "notification"}
            }
            save_state(state)
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError, ValueError):
            pass
def restore_volumes() -> None:
    state = load_state()
    saved = state.pop("saved_volumes", None)
    if isinstance(saved, dict):
        for stream, volume in saved.items():
            try:
                command("termux-volume", str(stream), str(int(volume)), timeout=5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        save_state(state)


def start_alarm_notification(reason: str, day: date | None = None) -> None:
    args = [
        "termux-notification",
        "--id",
        ALARM_NOTIFICATION_ID,
        "--ongoing",
        "--priority",
        "max",
        "--title",
        "立即关灯",
        "--content",
        f"{reason}。{quota_text(day, load_state()) if day else '请关灯'}；01:00停止",
    ]
    state = load_state()
    if day:
        add_action_buttons(args, day, state)
    best_effort(*args, timeout=8)


def alarm_level(elapsed: float) -> int:
    return min(15, 2 + int(max(0.0, elapsed) // 4) * 2)


def alarm_pulse(elapsed: float) -> None:
    save_volumes()
    best_effort("termux-volume", "music", str(alarm_level(elapsed)), timeout=5)
    best_effort("termux-media-player", "play", str(ALARM_FILE), timeout=6)
    if elapsed >= 12:
        vibration_ms = min(1800, 400 + int((elapsed - 12) * 70))
        best_effort("termux-vibrate", "-d", str(vibration_ms), "-f", timeout=5)


def stop_alarm() -> None:
    try:
        command("termux-media-player", "stop", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    remove_notification(ALARM_NOTIFICATION_ID)
    restore_volumes()


def session_stop_reason(day: date) -> str | None:
    now = datetime.now()
    state = load_state()
    if state.get("completed_date") == day_text(day):
        return "cancel"
    if now >= datetime.combine(day, CUTOFF_TIME):
        return "cutoff"
    if (delay_until(day, state) or datetime.min) > now:
        return "delay"
    return None


def alarm_until_dark(initial_reason: str, day: date | None = None) -> str:
    print(f"{datetime.now().isoformat(timespec='seconds')} ALARM {initial_reason}", flush=True)
    start_alarm_notification(initial_reason, day)
    dark_streak = 0
    next_pulse = 0.0
    started = time.monotonic()
    while dark_streak < 2:
        stop_reason = session_stop_reason(day) if day else None
        if stop_reason:
            stop_alarm()
            print(f"{datetime.now().isoformat(timespec='seconds')} {stop_reason.upper()} alarm stopped", flush=True)
            return stop_reason
        now = time.monotonic()
        if now >= next_pulse:
            alarm_pulse(now - started)
            next_pulse = now + 4.0
        sample = read_sample()
        if valid_dark(sample):
            dark_streak += 1
        else:
            dark_streak = 0
        time.sleep(0.25)
    stop_alarm()
    print(f"{datetime.now().isoformat(timespec='seconds')} DARK alarm stopped", flush=True)
    return "dark"


def classify_dark() -> tuple[bool, dict]:
    samples = [read_sample() for _ in range(2)]
    return all(valid_dark(sample) for sample in samples), samples[-1]


def monitor_guard(day: date, *, persist: bool = True) -> None:
    state = load_state()
    try:
        guard_until = datetime.fromisoformat(str(state.get("guard_until"))) if persist else None
    except (TypeError, ValueError):
        guard_until = None
    if guard_until is None:
        guard_until = datetime.now() + timedelta(seconds=GUARD_SECONDS)
        if persist:
            update_state(guard_until=guard_until.isoformat(timespec="seconds"))

    while datetime.now() < guard_until:
        if persist and session_stop_reason(day) in {"cancel", "cutoff"}:
            break
        is_dark, sample = classify_dark()
        if not is_dark:
            result = alarm_until_dark(sensor_reason(sample), day if persist else None)
            if result == "delay":
                until = delay_until(day)
                if until:
                    guard_until = min(
                        until + timedelta(seconds=GUARD_SECONDS),
                        datetime.combine(day, CUTOFF_TIME),
                    )
                    if persist:
                        update_state(guard_until=guard_until.isoformat(timespec="seconds"))
                    while datetime.now() < until:
                        time.sleep(min(5.0, (until - datetime.now()).total_seconds()))
                continue
            if result in {"cancel", "cutoff"}:
                break
            guard_until = datetime.now() + timedelta(seconds=GUARD_SECONDS)
            if persist:
                update_state(guard_until=guard_until.isoformat(timespec="seconds"))
        else:
            remaining = max(0, int((guard_until - datetime.now()).total_seconds()))
            print(f"{datetime.now().isoformat(timespec='seconds')} GUARD {remaining}s", flush=True)
            time.sleep(2)

    if persist:
        update_state(completed_date=day_text(day), guard_until=None)
    remove_notification(PREP_NOTIFICATION_ID)
    stop_alarm()
    print(f"{datetime.now().isoformat(timespec='seconds')} COMPLETE", flush=True)


def test_now() -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} TEST started", flush=True)
    is_dark, sample = classify_dark()
    if not is_dark:
        alarm_until_dark(sensor_reason(sample))
    monitor_guard(datetime.now().date(), persist=False)
    print(f"{datetime.now().isoformat(timespec='seconds')} TEST complete", flush=True)


def run_session(day: date) -> None:
    state = load_state()
    delayed = delay_until(day, state)
    deadline = delayed or datetime.combine(day, NORMAL_DEADLINE)

    if datetime.now() < datetime.combine(day, NORMAL_DEADLINE):
        notify_prepare(delayed=bool(delayed))

    cutoff = datetime.combine(day, CUTOFF_TIME)
    while datetime.now() < cutoff:
        while datetime.now() < deadline:
            if session_stop_reason(day) == "cancel":
                remove_notification(PREP_NOTIFICATION_ID)
                stop_alarm()
                return
            state = load_state()
            new_deadline = delay_until(day, state)
            if new_deadline and new_deadline != deadline:
                deadline = new_deadline
                notify_prepare(delayed=True)
            time.sleep(min(5.0, max(0.2, (deadline - datetime.now()).total_seconds())))

        remove_notification(PREP_NOTIFICATION_ID)
        is_dark, sample = classify_dark()
        if not is_dark:
            result = alarm_until_dark(sensor_reason(sample), day)
            if result == "delay":
                deadline = delay_until(day) or cutoff
                notify_prepare(delayed=True)
                continue
            if result in {"cancel", "cutoff"}:
                return
        break

    if datetime.now() >= cutoff:
        update_state(completed_date=day_text(day), guard_until=None)
        remove_notification(PREP_NOTIFICATION_ID)
        stop_alarm()
        return
    update_state(
        session_date=day_text(day),
        guard_until=(datetime.now() + timedelta(seconds=GUARD_SECONDS)).isoformat(timespec="seconds"),
    )
    monitor_guard(day)


def next_prep(now: datetime) -> datetime:
    candidate = datetime.combine(now.date(), PREP_TIME)
    if now >= candidate:
        candidate += timedelta(days=1)
    return candidate


def daemon() -> None:
    best_effort("termux-api-start", timeout=8)
    print(f"{datetime.now().isoformat(timespec='seconds')} DAEMON started", flush=True)
    while True:
        now = datetime.now()
        in_window = PREP_TIME <= now.time() < CUTOFF_TIME
        state = load_state()
        if in_window and state.get("completed_date") != day_text(now.date()):
            run_session(now.date())
            continue
        sleep_seconds = max(1.0, (next_prep(now) - now).total_seconds())
        time.sleep(min(sleep_seconds, 300.0))


def request_delay() -> int:
    now = datetime.now()
    state = load_state()
    allowed_time = PREP_TIME <= now.time() < CUTOFF_TIME
    count = weekly_delay_count(now.date(), state)
    available = count < MAX_WEEKLY_DELAYS
    if allowed_time and available:
        until = min(
            now + timedelta(minutes=DELAY_MINUTES),
            datetime.combine(now.date(), CUTOFF_TIME),
        )
        update_state(
            delay_week=week_key(now.date()),
            delay_count=count + 1,
            delay_date=day_text(now.date()),
            delay_until=until.isoformat(timespec="seconds"),
        )
        stop_alarm()
        notify_prepare(delayed=True)
        return 0
    message = "只能在 00:25 至 01:00 之间延迟" if not allowed_time else "本周3次延迟机会已经用完"
    command("termux-toast", message, timeout=5)
    return 1


def request_cancel() -> int:
    now = datetime.now()
    state = load_state()
    allowed_time = PREP_TIME <= now.time() < CUTOFF_TIME
    available = state.get("cancel_week") != week_key(now.date())
    if allowed_time and available:
        update_state(
            cancel_week=week_key(now.date()),
            canceled_date=day_text(now.date()),
            completed_date=day_text(now.date()),
            guard_until=None,
        )
        remove_notification(PREP_NOTIFICATION_ID)
        stop_alarm()
        command("termux-toast", "今晚检测已取消", timeout=5)
        return 0
    message = "只能在 00:25 至 01:00 之间取消" if not allowed_time else "本周取消机会已经使用"
    command("termux-toast", message, timeout=5)
    return 1


def calibrate_dark() -> int:
    samples = [read_sample() for _ in range(7)]
    if not all(sample.get("ok") for sample in samples):
        print("校准失败：传感器读取异常", file=sys.stderr)
        return 1
    if not all(sample["proximity"] >= PROXIMITY_FAR_MIN for sample in samples):
        print("校准失败：距离传感器被遮挡", file=sys.stderr)
        return 1
    if not all(sample["gravity"][1] >= UPRIGHT_Y_MIN for sample in samples):
        print("校准失败：请保持手机竖直摆放", file=sys.stderr)
        return 1
    lights = [sample["light"] for sample in samples]
    median = statistics.median(lights)
    if median > 20:
        print(f"校准失败：当前仍像是开灯状态（{median:.1f} lux）", file=sys.stderr)
        return 1
    threshold = min(20.0, max(2.0, math.ceil(median + 2.0)))
    update_state(dark_max=threshold)
    print(f"暗环境校准完成：阈值 {threshold:.1f} lux")
    return 0


def status() -> None:
    now = datetime.now()
    state = load_state()
    payload = {
        "now": now.isoformat(timespec="seconds"),
        "dark_max": dark_threshold(),
        "weekly_delays_used": weekly_delay_count(now.date(), state),
        "weekly_delays_remaining": max(0, MAX_WEEKLY_DELAYS - weekly_delay_count(now.date(), state)),
        "weekly_cancel_available": state.get("cancel_week") != week_key(now.date()),
        "delay_date": state.get("delay_date"),
        "completed_date": state.get("completed_date"),
        "guard_until": state.get("guard_until"),
        "sample": read_sample(),
    }
    payload["sample_reason"] = sensor_reason(payload["sample"])
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def self_test() -> None:
    upright_dark = {"ok": True, "light": 0, "proximity": 5, "gravity": [0, 9.4, 2.8]}
    covered = {"ok": True, "light": 0, "proximity": 0, "gravity": [0, 9.4, 2.8]}
    face_down = {"ok": True, "light": 0, "proximity": 5, "gravity": [0, 0, 9.8]}
    lit = {"ok": True, "light": 60, "proximity": 5, "gravity": [0, 9.4, 2.8]}
    assert valid_dark(upright_dark, 5)
    assert not valid_dark(covered, 5)
    assert not valid_dark(face_down, 5)
    assert not valid_dark(lit, 5)
    assert week_key(date(2026, 9, 14)) == "2026-W38"
    assert weekly_delay_count(date(2026, 9, 14), {}) == 0
    assert weekly_delay_count(date(2026, 9, 14), {"delay_week": "2026-W38", "delay_count": 2}) == 2
    assert alarm_level(0) == 2
    assert alarm_level(12) == 8
    assert alarm_level(30) == 15
    print("PASS: late-night guard logic")


def cleanup(*_: object) -> None:
    stop_alarm()
    raise SystemExit(0)


def main() -> int:
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    mode = sys.argv[1] if len(sys.argv) > 1 else "daemon"
    if mode == "daemon":
        daemon()
    elif mode == "delay":
        return request_delay()
    elif mode == "cancel":
        return request_cancel()
    elif mode == "status":
        status()
    elif mode == "sample":
        print(json.dumps(read_sample(), ensure_ascii=False, indent=2))
    elif mode == "calibrate-dark":
        return calibrate_dark()
    elif mode == "self-test":
        self_test()
    elif mode == "test-now":
        test_now()
    else:
        print("用法: late-night-guard.py [daemon|delay|cancel|status|sample|calibrate-dark|self-test|test-now]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
