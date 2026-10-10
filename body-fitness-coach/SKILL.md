---
name: body-fitness-coach
description: Use when the user discusses meal logging, workouts, Xiaomi or Xunji fitness data, recovery, body metrics, daily reports, weekly reports, or personal fitness coaching.
---

# Body Fitness Coach

Use `scripts/coach.py` relative to this skill. Private state belongs in `BODY_FITNESS_DATA_DIR/private.db`; never place identifiers, measurements, tokens, or exported personal data in this skill folder.

## Choose the operation

| Request | Action |
| --- | --- |
| Analyze data or preview a report | Use `daily --dry-run`, `weekly --dry-run`, or `doctor`; these may read sources but must not send or write externally. |
| Send a daily/weekly report | Ask for confirmation immediately before running without `--dry-run`; use `daily --force` only for an explicitly requested resend. |
| Record food from text/photo | Extract meal type, food, amount, unit, date, and nutrition estimate; show uncertain recognition for confirmation. Then pass JSON through `food --json-file ... --source-id ...`. |
| Change a training template | Show the concrete diff and obtain separate confirmation. Reports may recommend changes but never apply them. |

Food JSON must contain `foods`; every item needs `meal_type`, `name`, `amount`, `unit`, `date`, and `ntr`. Do not invent missing portions. Photos with ambiguous foods or quantities require user confirmation.

The strength split advances only after a recorded standalone strength session. Fixed activities, missed sessions, and report generation do not advance it.

Daily reports show body change only when a new measurement arrives; do not repeat the same measurement. Daily coaching must weigh all available body change, sleep, activity, completed training, and diet data together, then use one or two short sentences for the highest-priority action without mechanically repeating report data or adding a mental-wellbeing section. Weekly coaching may be more detailed and should separate recovery, training, and nutrition trends. It is not medical diagnosis. Persistent pain, swelling, numbness, weakness, chest symptoms, fainting, or disordered-eating signs require professional evaluation rather than training escalation.

Run `python scripts/coach.py --help` for commands. Run `doctor` for missing credentials, CLI availability, Xiaomi login, or model fallback causes. Install or migrate with `powershell -File scripts/install.ps1`; preview consequential changes with `-WhatIf` first.
