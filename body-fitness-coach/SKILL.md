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
| Record food from text/photo | Infer meal type, foods, portions, and nutrition from the message, photo, time, and recent context, then write immediately without confirmation. Ask only when the meal cannot be identified well enough to create a useful record. Then pass JSON through `food --json-file ... --source-id ...`. |
| Record personal body knowledge | When the user provides a report, measurement, body image, supplement, available equipment, medical history, symptom, or any other durable fact useful for future body/training advice, record it locally without asking for a second confirmation. Follow the knowledge workflow below. |
| View or use personal body knowledge | Use `knowledge status`, `knowledge search`, or `knowledge show`. Retrieve only the relevant entries for the current task. |
| Change a training template | Show the concrete diff and obtain separate confirmation. After approval, update the living Lark training manual, append its dated change record, record the same change in the private knowledge base, and sync the mirror. Reports may recommend changes but never apply them. |

Food JSON must contain `foods`; every item needs `meal_type`, `name`, `amount`, `unit`, `date`, and `ntr`. Prefer a reasonable ordinary-serving estimate over a clarification round, and state the important portion or nutrition assumptions after writing so the user can correct them. After every food write, report calories, protein, carbohydrates, and fat for both the recorded meal and the day's cumulative total, alongside the daily targets when available.

The strength split advances only after a recorded standalone strength session. Fixed activities, missed sessions, and report generation do not advance it.

Daily reports show body change only when a new measurement arrives; do not repeat the same measurement. Daily coaching must weigh all available body change, sleep, activity, completed training, and diet data together, then use one or two short sentences for the highest-priority action without mechanically repeating report data or adding a mental-wellbeing section. Weekly coaching may be more detailed and should separate recovery, training, and nutrition trends. Every weekly report must also give a concrete next-week schedule and an explicit template decision: if unchanged, name the current template and explain why; if a change is recommended, show the exact before/after action or set/rep difference, the evidence, and that confirmation is pending. Never substitute a policy-only template line for an actual recommendation. It is not medical diagnosis. Persistent pain, swelling, numbness, weakness, chest symptoms, fainting, or disordered-eating signs require professional evaluation rather than training escalation.

For current weight, body composition, calorie targets, protein targets, and training advice, use the newest Xiaomi body measurement. Treat measurements in examination reports and other dated documents as historical snapshots unless they are newer than Xiaomi data. Compare timestamps before calculating and state the measurement date when the value materially affects advice.

## Personal body knowledge workflow

The private knowledge base is `.private/knowledge/BODY_KNOWLEDGE.md`. Original PDFs, images, and other files are deduplicated by SHA-256 under `.private/knowledge/sources/`; searchable metadata and normalized metrics stay in the existing `.private/private.db`. `.private/` is gitignored. Never copy personal source material into tracked files.

Do not use a fixed category list. For every new item:

1. Decide whether it is a durable fact that may change future health, recovery, nutrition, equipment, or training advice. Ordinary meals, workouts, Xiaomi data, and Xunji data remain in their existing systems and are not duplicated here.
2. Run `knowledge status`, then `knowledge search --query <specific topic>` for comparable history. Reuse an existing primary category when its decision purpose matches; otherwise create one concise new category. Add multiple tags as needed, but only one primary category.
3. Extract and clearly separate source facts, comparison with similar history, analysis, and uncertain or unverified items. Never present an inference as a source fact. Normalize comparable numeric metrics and include their unit, reference range, abnormal flag, and source page when available.
4. For a multi-page PDF, use the PDF skill to inspect every page. Work in batches of roughly 5–10 pages when needed, retain the original file, record total page count and processed pages, and do not store full OCR text in the Markdown. The original PDF remains the source of truth.
5. Write one JSON payload and run `knowledge add --json-file ... --source-id ... --source <original-file>`. Use the bridge message ID as `source-id` when available. The command deduplicates originals, adds exact-name/unit metric comparisons, regenerates the readable document, and attempts a Lark mirror sync.
6. Give the user a concise comparison and analysis after recording. For medical material, explain uncertainty and recommend professional review when appropriate; do not diagnose.

Knowledge JSON fields are: required `record_date`, `title`, and `category`; optional `tags`, `facts`, `comparison`, `analysis`, `uncertainty`, `metrics`, `current`, `page_count`, `pages_processed`, and `source_files`. `current` is a key/value map for facts that should appear in the current snapshot; a newer entry with the same key replaces it, and an empty value removes it. A metric is `{name,value,unit,reference_range,abnormal,page}`.

Daily reports, weekly reports, meal reminders, and their model prompts must never read the knowledge tables or `BODY_KNOWLEDGE.md`. Only retrieve targeted knowledge when the user's task actually needs it. `knowledge show` is for an explicit full review; normally prefer a narrow `knowledge search` to reduce token use.

The local Markdown is the primary readable record. Its Lark document is a one-way mirror: a local write succeeds even if Lark is unavailable, failed sync is marked pending, and the next knowledge operation retries once a document is configured. Raw source files are not uploaded. Create or configure the mirror with `knowledge sync --create` or `knowledge sync --doc <url-or-token>`. Never start Lark OAuth from a group chat; ask the user to authorize in a private chat if user identity is unavailable.

The living strength-training manual is a separate editable Lark document. Configure its URL with `knowledge sync --manual-doc <url>`, which places a persistent link before the knowledge snapshot. When the user confirms a training-template change, update the applicable current-plan section in that manual and append a dated item under `更新记录` containing the before/after change and reason; then add the same confirmed change as a `训练方案` knowledge entry and sync the mirror. Keep imported source files as immutable evidence rather than rewriting them. If the manual update fails, report that the change was not fully applied.

When a scheduled weekly report was missed while the computer was unavailable, the next weekly run sends every unsent completed week oldest-first and saves progress after each successful card. A first run sends only the latest completed week.

Run `.private/.venv/Scripts/python.exe scripts/coach.py --help` for commands. Run `doctor` for history location, missing credentials, CLI availability, Xiaomi login, or model fallback causes. Install or migrate with `powershell -File scripts/install.ps1`; preview consequential changes with `-WhatIf` first.
