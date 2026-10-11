---
name: body-fitness-coach
description: Use when the user discusses meal logging, workouts, Xiaomi or Xunji fitness data, recovery, body metrics, daily reports, weekly reports, or personal fitness coaching.
---

# Body Fitness Coach

Use `.private/.venv/Scripts/python.exe scripts/coach.py` relative to this skill. The script defaults to `.private/private.db`, so do not pass `--data-dir` for the normal local installation. `.private/` is gitignored and is the only place in this folder allowed to contain identifiers, measurements, runtime files, or exported personal data; never place them in tracked files.

When explicitly activated in a fresh or reset session, run `doctor` once. Confirm that its `state` points to this skill's `.private/private.db` and that `history` is non-empty before recording, analyzing, or sending anything. If `.private` or its interpreter is missing, run `powershell -File scripts/install.ps1`; do not create a second data directory.

## Choose the operation

| Request | Action |
| --- | --- |
| Analyze data or preview a report | Use `daily --dry-run`, `weekly --dry-run`, or `doctor`; these may read sources but must not send or write externally. |
| Send a daily/weekly report | Ask for confirmation immediately before running without `--dry-run`; use `daily --force` only for an explicitly requested resend. |
| Record food from text/photo | For a text breakfast/lunch/dinner log, extract meal type, food, amount, unit, date, and nutrition estimate, then write it immediately without asking for confirmation. For photos, show uncertain recognition for confirmation. Then pass JSON through `food --json-file ... --source-id ...`. |
| Change a training template | Show the concrete diff and obtain separate confirmation. Reports may recommend changes but never apply them. |

Food JSON must contain `foods`; every item needs `meal_type`, `name`, `amount`, `unit`, `date`, and `ntr`. Do not invent missing portions. Text meal logs are pre-authorized for immediate recording; state any ordinary serving-size or nutrition estimates in the completion message instead of pausing for confirmation. Photos with ambiguous foods or quantities require user confirmation.

The strength split advances only after a recorded standalone strength session. Fixed activities, missed sessions, and report generation do not advance it.

Daily reports show body change only when a new measurement arrives; do not repeat the same measurement. Daily coaching must weigh all available body change, sleep, activity, completed training, and diet data together, then use one or two short sentences for the highest-priority action without mechanically repeating report data or adding a mental-wellbeing section. Weekly coaching may be more detailed and should separate recovery, training, and nutrition trends. It is not medical diagnosis. Persistent pain, swelling, numbness, weakness, chest symptoms, fainting, or disordered-eating signs require professional evaluation rather than training escalation.

Run `.private/.venv/Scripts/python.exe scripts/coach.py --help` for commands. Run `doctor` for history location, missing credentials, CLI availability, Xiaomi login, or model fallback causes. Install or migrate with `powershell -File scripts/install.ps1`; preview consequential changes with `-WhatIf` first.
