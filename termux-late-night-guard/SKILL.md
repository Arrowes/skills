---
name: termux-late-night-guard
description: Deploy, configure, test, or troubleshoot a no-root Android Termux bedtime alarm that uses light, proximity, and gravity sensors, supports limited weekly delay/cancel actions, and recovers from Termux:API failures. Use for installing this guard on an old phone or maintaining an existing deployment; do not use for generic Android alarms or medical sleep monitoring.
---

# Termux Late Night Guard

Deploy the bundled sensor-based bedtime guard without building an Android app.

## Preconditions

- Use a compatible Termux installation plus the matching Termux:API Android plugin and `termux-api` package.
- Confirm notification, sensor, media, vibration, and storage permissions before deployment.
- On MIUI or similar systems, set Termux, Termux:API, and Termux:Boot to allow autostart and unrestricted background battery use.
- Treat host addresses, SSH credentials, and device-specific sensor thresholds as deployment inputs. Never add them to this skill or a public repository.

## Bundled Scripts

- `scripts/late-night-guard.py`: detection, alarm, weekly quota, API recovery, calibration, and test modes.
- `scripts/start-late-night-guard`: idempotent Termux:Boot launcher.

The script installs at `~/server/late-night-guard.py`; the launcher installs at `~/.termux/boot/start-late-night-guard`.

## Deployment

1. Inspect the target device and existing files. Back up an existing deployment before replacement.
2. Copy the two bundled scripts to their install paths and set mode `700`.
3. Run `python ~/server/late-night-guard.py self-test`.
4. Start `~/.termux/boot/start-late-night-guard` and verify exactly one `late-night-guard.py daemon` process.
5. With the phone upright and uncovered in the intended dark room, run `calibrate-dark`; do not reuse another device's lux threshold.
6. Run `status` and confirm light, proximity, and gravity samples are valid.

Preserve `~/.local/state/late-night-guard/state.json` during upgrades so calibration and weekly quotas survive.

## Behavior Contract

- Prepare at 00:25; enforce from 00:30; stop unconditionally at 01:00.
- Valid darkness requires low light, an uncovered proximity sensor, upright gravity, and two consecutive valid samples.
- After darkness, monitor for relighting for 15 minutes.
- `延迟15分` stops the alarm and delays for 15 minutes, capped at three uses per ISO week and never beyond 01:00.
- `取消今晚` ends the current night's session, capped at one use per ISO week.
- Alarm audio ramps from low to full volume over roughly 30 seconds; vibration begins after 12 seconds.
- Notification/API failure must not terminate the core daemon. The script restarts Termux:API and retries once.

## Verification and Operations

```bash
python ~/server/late-night-guard.py self-test
python ~/server/late-night-guard.py sample
python ~/server/late-night-guard.py status
python ~/server/late-night-guard.py test-now
```

`test-now` does not consume weekly quotas or mark the night complete. Stop a simulation with:

```bash
pkill -f '[l]ate-night-guard.py test-now'
termux-media-player stop
```

After stopping a test, verify the simulation count is zero, the daemon count is one, and saved volumes were restored.

When diagnosing a missed alarm, check device time, daemon count, status JSON, and `~/server/logs/late-night-guard.log` before changing configuration. A stuck `termux-api Notification` process indicates the Android plugin is unresponsive; restart Termux:API and retain the script's timeout fallback.

## Android Boundary

Termux:Boot can recover after a normal phone reboot, and an interactive shell hook may rerun the launcher when Termux opens. Android's Force stop state cannot be bypassed without the user reopening Termux or changing system state. Report this limit instead of claiming guaranteed self-recovery.
