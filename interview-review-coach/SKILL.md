---
name: interview-review-coach
description: Use when reviewing job interview recordings, transcripts, interview notes, Q&A summaries, self-introductions, suggested answers, technical interview gaps, or follow-up study plans for computer vision, autonomous driving perception, BEV, deployment, or algorithm engineering roles.
---

# Interview Review Coach

## Overview

Turn interview recordings or transcripts into a reusable interview-prep asset: accurate answer records, stronger suggested answers, exposed knowledge gaps, and next-interview study priorities.

## First Steps

1. Locate the source material: audio files, raw transcripts, segment JSON, existing recap Markdown, resume, or job description.
2. Preserve the source truth. Mark unclear, low-volume, or overlapping speech as uncertain instead of inventing details.
3. If audio transcription is needed, run the built-in `scripts/transcribe_interview.py` helper first; use project-specific scripts only when they are clearly better for the source material.
4. Create or update a Markdown recap near the source files unless the user specifies another location.
5. Use `references/interview-output-format.md` for the output contract.
6. Load `references/perception-knowledge-map.md` when the interview involves autonomous driving, CV, BEV, model deployment, or edge-AI topics.

## Built-in Transcription Script

When audio transcription is needed and no transcript or segment JSON exists, run `scripts/transcribe_interview.py` from this skill folder before summarizing.

```bash
python scripts/transcribe_interview.py path/to/interview.mp3 --language zh --model small
```

The helper writes `<audio>.transcript.md` unless `--output` is set. It auto-selects `faster-whisper`, `whisper`, or `whisper-cli`, and reports a clear error if no backend is installed. Use `--allow-download` only when model downloads are acceptable; otherwise pass a local model path or use a cached model.

## Output Contract

Every full recap should include:

1. Interview metadata: company, date, source files, uncertainty notes.
2. Q&A sections: `问题`, `我的回答`, `建议回答`.
3. Overall performance summary.
4. Weaknesses exposed by the interview.
5. Knowledge-point review and likely follow-up questions.
6. Next-interview priorities.

For partial tasks, update only the requested sections while preserving the same wording rules.

## Voice Rules

The `我的回答` section is a cleaned record of the user's actual answer. It must read like the answer itself, not a third-party commentary.

Use direct record style:

```markdown
**我的回答**

我主要负责泊车场景下的四路鱼眼 BEV 感知，包括多任务目标检测、车位检测、车道线检测和高度预测。
```

Avoid third-person or reporting labels inside `我的回答`:

```markdown
回答中提到主要负责泊车场景...
候选人表示自己参与了...
你说你主要负责...
```

Keep interviewer questions faithful. If the original question says `你` or `你们`, keep it in `问题`.

In analysis and suggested-answer sections, avoid second-person scolding. Prefer neutral labels such as `现场回答可以补充`, `建议加强`, `更完整的口径是`.

## Suggested Answer Rules

Suggested answers may improve structure, precision, and interviewer confidence, but must not fabricate project ownership, metrics, tools, platforms, publications, or company experience.

When strengthening an answer:

- Lead with the direct answer.
- Add technical reasoning.
- Add engineering tradeoffs.
- Add concrete project context only if present in source material or resume.
- Add uncertainty language when the source is ambiguous.
- Keep answers interview-ready, not textbook-like.

## Knowledge Gap Review

For technical interviews, extract each exposed knowledge point into:

- `必须掌握`: the minimum answer expected.
- `容易追问`: likely follow-up questions.
- `优秀回答要点`: what a strong candidate should mention.
- `容易踩坑`: weak or risky responses.
- `复习优先级`: high, medium, or low.

Use the perception knowledge map for common topics, but adapt to the actual interview.

## Quality Checks

Before finishing:

1. Search the recap for forbidden phrasing in `我的回答`: `回答中提到`, `候选人`, `你说`, `你介绍`, `你回答`.
2. Confirm `问题 / 我的回答 / 建议回答` headings are consistent.
3. Confirm unclear transcript parts are marked instead of guessed.
4. Confirm suggested answers do not claim experience beyond the source.
5. Confirm the final section tells the user what changed and where the file is.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Turning actual answers into third-person summaries | Rewrite `我的回答` as direct answer text |
| Removing `你` from interviewer questions | Preserve original question wording |
| Producing only polished answers | Include the original answer record first |
| Making knowledge notes too broad | Tie every review point to a question or exposed weakness |
| Inventing metrics to sound stronger | Use only verified metrics or write a metric-free answer |
