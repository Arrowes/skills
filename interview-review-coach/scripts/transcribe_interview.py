#!/usr/bin/env python3
"""Transcribe an interview recording into timestamped Markdown."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    whole_seconds, millis = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{millis:03d}"


def compact_text(text: str) -> str:
    return " ".join(text.split())


def normalize_segments(raw_segments: Iterable[Any]) -> list[Segment]:
    segments: list[Segment] = []
    for raw in raw_segments:
        if isinstance(raw, dict):
            start = raw.get("start", 0)
            end = raw.get("end", start)
            text = raw.get("text", "")
        else:
            start = getattr(raw, "start", 0)
            end = getattr(raw, "end", start)
            text = getattr(raw, "text", "")

        cleaned = compact_text(str(text))
        if cleaned:
            segments.append(Segment(float(start), float(end), cleaned))
    return segments


def render_markdown(
    audio_path: Path,
    segments: Iterable[Segment],
    backend: str,
    model: str,
    language: str | None,
) -> str:
    lines = [
        "# Interview Transcript",
        "",
        f"- Source: `{audio_path}`",
        f"- Backend: `{backend}`",
        f"- Model: `{model}`",
        f"- Language: `{language or 'auto'}`",
        "",
        "## Transcript",
        "",
    ]

    for segment in segments:
        text = compact_text(segment.text)
        if text:
            lines.append(
                f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] {text}"
            )

    return "\n".join(lines).rstrip() + "\n"


def available_backends() -> list[str]:
    backends: list[str] = []
    if importlib.util.find_spec("faster_whisper"):
        backends.append("faster-whisper")

    whisper_spec = importlib.util.find_spec("whisper")
    if whisper_spec is not None:
        backends.append("whisper")

    if shutil.which("whisper"):
        backends.append("whisper-cli")

    return backends


def choose_backend(requested: str, available: list[str] | None = None) -> str:
    choices = available_backends() if available is None else available
    if requested == "auto":
        if choices:
            return choices[0]
        raise RuntimeError(
            "No transcription backend found. Install faster-whisper or openai-whisper, "
            "or put a whisper CLI on PATH."
        )

    if requested in choices:
        return requested

    raise RuntimeError(
        f"Requested backend '{requested}' is not available. Available: "
        f"{', '.join(choices) if choices else 'none'}."
    )


def transcribe_with_faster_whisper(args: argparse.Namespace) -> tuple[list[Segment], str | None]:
    from faster_whisper import WhisperModel

    kwargs: dict[str, Any] = {
        "device": args.device,
        "compute_type": args.compute_type,
    }
    if not args.allow_download:
        kwargs["local_files_only"] = True

    model = WhisperModel(args.model, **kwargs)
    segments, info = model.transcribe(
        str(args.audio),
        language=args.language,
        task=args.task,
        vad_filter=args.vad_filter,
    )
    return normalize_segments(segments), getattr(info, "language", args.language)


def transcribe_with_whisper(args: argparse.Namespace) -> tuple[list[Segment], str | None]:
    import whisper

    model = whisper.load_model(args.model)
    result = model.transcribe(
        str(args.audio),
        language=args.language,
        task=args.task,
    )
    return normalize_segments(result.get("segments", [])), result.get("language", args.language)


def transcribe_with_whisper_cli(args: argparse.Namespace) -> tuple[list[Segment], str | None]:
    executable = shutil.which("whisper")
    if executable is None:
        raise RuntimeError("whisper CLI is not on PATH.")

    with tempfile.TemporaryDirectory(prefix="interview-transcript-") as tmp:
        command = [
            executable,
            str(args.audio),
            "--model",
            args.model,
            "--task",
            args.task,
            "--output_format",
            "json",
            "--output_dir",
            tmp,
        ]
        if args.language:
            command.extend(["--language", args.language])

        subprocess.run(command, check=True)
        json_files = list(Path(tmp).glob("*.json"))
        if not json_files:
            raise RuntimeError("whisper CLI finished without writing a JSON transcript.")

        data = json.loads(json_files[0].read_text(encoding="utf-8"))
    return normalize_segments(data.get("segments", [])), data.get("language", args.language)


def transcribe(args: argparse.Namespace, backend: str) -> tuple[list[Segment], str | None]:
    if backend == "faster-whisper":
        return transcribe_with_faster_whisper(args)
    if backend == "whisper":
        return transcribe_with_whisper(args)
    if backend == "whisper-cli":
        return transcribe_with_whisper_cli(args)
    raise RuntimeError(f"Unsupported backend: {backend}")


def default_output_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(audio_path.suffix + ".transcript.md")


def run_self_test() -> None:
    assert format_timestamp(61.2) == "00:01:01.200"
    assert choose_backend("auto", available=["faster-whisper"]) == "faster-whisper"

    segments = normalize_segments(
        [
            {"start": 0, "end": 1.25, "text": "  hello   world "},
            {"start": 3, "end": 4, "text": ""},
        ]
    )
    assert segments == [Segment(0.0, 1.25, "hello world")]

    rendered = render_markdown(Path("demo.wav"), segments, "test", "tiny", None)
    assert "[00:00:00.000 - 00:00:01.250] hello world" in rendered
    print("self-test passed")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe an interview audio/video file to timestamped Markdown."
    )
    parser.add_argument("audio", nargs="?", type=Path, help="Audio or video file to transcribe.")
    parser.add_argument("--output", type=Path, help="Markdown output path.")
    parser.add_argument(
        "--backend",
        choices=["auto", "faster-whisper", "whisper", "whisper-cli"],
        default="auto",
        help="Transcription backend. Default: auto.",
    )
    parser.add_argument("--model", default="small", help="Whisper model name or local path.")
    parser.add_argument("--language", help="Language code such as zh or en. Default: auto.")
    parser.add_argument(
        "--task",
        choices=["transcribe", "translate"],
        default="transcribe",
        help="Whisper task. Default: transcribe.",
    )
    parser.add_argument("--device", default="auto", help="faster-whisper device. Default: auto.")
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="faster-whisper compute type. Default: int8.",
    )
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="Allow a backend to download the model if it is not already cached.",
    )
    parser.add_argument(
        "--vad-filter",
        action="store_true",
        help="Enable faster-whisper VAD filtering for long recordings.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run built-in checks and exit.")

    args = parser.parse_args(argv)
    if args.self_test:
        return args
    if args.audio is None:
        parser.error("audio is required unless --self-test is used")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        run_self_test()
        return 0

    if not args.audio.exists():
        raise SystemExit(f"Audio file not found: {args.audio}")

    backend = choose_backend(args.backend)
    segments, detected_language = transcribe(args, backend)
    output_path = args.output or default_output_path(args.audio)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_markdown(args.audio, segments, backend, args.model, detected_language),
        encoding="utf-8",
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
