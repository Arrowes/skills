#!/usr/bin/env python3
"""Minimal local fitness coach: sync, summarize, report, and guarded writes."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
import getpass
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SKILL_ROOT = ROOT.parent
DEFAULT_DATA_DIR = SKILL_ROOT / ".private"
KEYRING_SERVICE = "body-coach"
TOKEN_RE = re.compile(r"xj(?:llm|tpl|food|body)_[A-Za-z0-9_-]+")
SEARCH_KEY_RE = re.compile(r"Bearer\s+([a-f0-9]{32,})")


def _default_data_dir() -> Path:
    configured = os.environ.get("BODY_FITNESS_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_DATA_DIR.resolve()


DATA_DIR = _default_data_dir()
STATE_PATH = DATA_DIR / "private.db"


def set_data_dir(path: Path) -> None:
    global DATA_DIR, STATE_PATH
    DATA_DIR = path.expanduser().resolve()
    STATE_PATH = DATA_DIR / "private.db"


def load_config() -> dict[str, Any]:
    bundled_mi = Path(sys.executable).with_name("mi-fitness-mcp.exe" if os.name == "nt" else "mi-fitness-mcp")
    defaults = {
        "chat_id": "",
        "lark_cli": str(Path.home() / "AppData/Roaming/npm/lark-cli.cmd"),
        "codex_cli": str(Path.home() / "AppData/Roaming/npm/codex.cmd"),
        "mi_cli": str(bundled_mi) if bundled_mi.exists() else "mi-fitness-mcp",
        "timezone": "Asia/Shanghai",
        "model_analysis": True,
    }
    with db() as conn:
        row = conn.execute("select value from meta where key='config'").fetchone()
    if row:
        defaults.update(json.loads(row[0]))
    configured_mi = Path(str(defaults["mi_cli"]))
    if not configured_mi.exists() and configured_mi.name.lower() in {"mi-fitness-mcp", "mi-fitness-mcp.exe"} and bundled_mi.exists():
        defaults["mi_cli"] = str(bundled_mi)
    return defaults


def save_config(config: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            "insert into meta(key,value) values('config',?) on conflict(key) do update set value=excluded.value",
            (json.dumps(config, ensure_ascii=False, sort_keys=True),),
        )


def missing_dates(last_sent: date | None, today: date) -> list[date]:
    if not last_sent:
        return []
    current = last_sent + timedelta(days=1)
    result = []
    while current < today:
        result.append(current)
        current += timedelta(days=1)
    return result


def diet_auto_allowed(food: dict[str, Any]) -> bool:
    if food.get("analyze_only"):
        return False
    return bool(
        food.get("meal_type")
        and food.get("name")
        and food.get("amount") is not None
        and food.get("unit")
    )


def normalize_food_units(foods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for food in foods:
        amount, unit, ntr = food.get("amount"), str(food.get("unit", "")).lower(), food.get("ntr", {})
        if not isinstance(amount, (int, float)) or unit in ("g", "克"):
            continue
        match = next((item for item in ntr.get("foodUnit", []) if str(item.get("unit", "")).lower() == unit and isinstance(item.get("gram"), (int, float))), None)
        if match:
            food["amount"], food["unit"] = amount * match["gram"], "g"
    return foods


def request_id(kind: str, source_id: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{source_id}\n{raw}".encode()).hexdigest()[:24]
    return f"bc-{kind[:8]}-{digest}"[:50]


def command_prefix(command: str) -> list[str]:
    path = Path(command)
    entries = {
        "lark-cli": path.parent / "node_modules/@larksuite/cli/scripts/run.js",
        "codex": path.parent / "node_modules/@openai/codex/bin/codex.js",
    }
    entry = entries.get(path.stem.lower())
    if os.name == "nt" and path.suffix.lower() == ".cmd" and entry and entry.exists():
        node = path.parent / "node.exe"
        return [str(node) if node.exists() else "node", str(entry)]
    return [command]


def ensure_lark_channel_env(profile: str = "codex") -> None:
    home = Path(os.environ.get("LARK_CHANNEL_HOME", Path.home() / ".lark-channel"))
    os.environ.setdefault("LARK_CHANNEL", "1")
    os.environ.setdefault("LARK_CHANNEL_HOME", str(home))
    os.environ.setdefault("LARK_CHANNEL_PROFILE", profile)
    profile_root = home / "profiles" / os.environ["LARK_CHANNEL_PROFILE"]
    os.environ.setdefault("LARK_CHANNEL_CONFIG", str(profile_root / "lark-cli-source" / "config.json"))
    os.environ.setdefault("LARKSUITE_CLI_CONFIG_DIR", str(profile_root / "lark-cli"))


def request_json(url: str, method: str = "GET", timeout: int = 20) -> dict[str, Any]:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def login_xiaomi(base_url: str, timeout_seconds: int = 300) -> int:
    from mi_fitness_mcp.auth import save_mi_fitness_token
    from mi_fitness_mcp.security import load_api_key_secret

    try:
        session = request_json(f"{base_url.rstrip('/')}/api/auth/qr/start?region=cn", "POST")
    except OSError as exc:
        raise RuntimeError("Xiaomi login API is unavailable; start the local mi-fitness API first") from exc
    webbrowser.open(session["login_url"])
    print("xiaomi_login_opened_in_local_browser", flush=True)
    token = urllib.parse.quote(session["qr_token"], safe="")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(3)
        result = request_json(f"{base_url.rstrip('/')}/api/auth/qr/poll?token={token}", timeout=90)
        status = result.get("status")
        if status == "confirmed":
            pass_token = load_api_key_secret(result["api_key"])
            if not pass_token:
                raise RuntimeError("Xiaomi did not return a reusable passToken")
            save_mi_fitness_token(str(result["user_id"]), pass_token)
            print("xiaomi_credentials_saved")
            return 0
        if status not in {"waiting", "pending", "scanned"}:
            raise RuntimeError(f"Xiaomi login status: {status}")
    raise TimeoutError("Xiaomi login timed out")


def find_messages(value: Any, wanted: set[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            item_id = item.get("message_id") or item.get("messageId") or item.get("id")
            if item_id in wanted:
                found[item_id] = item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def unique_tokens(value: Any) -> list[str]:
    text = json.dumps(value, ensure_ascii=False)
    return list(dict.fromkeys(TOKEN_RE.findall(text) + SEARCH_KEY_RE.findall(text)))


def import_xunji_keys(chat_id: str, mappings: dict[str, str], start: str, inspect: bool = False) -> int:
    if not chat_id or not mappings:
        raise ValueError("chat ID and message mappings are required")
    ensure_lark_channel_env()
    config = load_config()
    command = command_prefix(config["lark_cli"]) + [
        "im", "+chat-messages-list", "--as", "bot", "--chat-id", chat_id,
        "--start", start, "--order", "asc", "--page-all", "--page-limit", "40",
        "--no-reactions", "--json",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
    messages = find_messages(json.loads(result.stdout), set(mappings.values()))
    extracted: dict[str, str] = {}
    for name, message_id in mappings.items():
        tokens = unique_tokens(messages.get(message_id, {}))
        if name == "diet":
            prefixed = [token for token in tokens if token.startswith("xj")]
            search = [token for token in tokens if re.fullmatch(r"[a-f0-9]{32,}", token)]
            if len(prefixed) != 1 or len(search) != 1:
                raise RuntimeError("diet mapping must contain one diet key and one search key")
            extracted["xunji_diet_key"], extracted["xunji_food_search_key"] = prefixed[0], search[0]
        elif len(tokens) == 1:
            extracted[name] = tokens[0]
        else:
            raise RuntimeError(f"{name}: expected one key, found {len(tokens)}")
    if not inspect:
        for name, token_value in extracted.items():
            secret_set(name, token_value)
    action = "found" if inspect else "stored"
    print(json.dumps({"action": action, "count": len(extracted), "keys": sorted(extracted)}, ensure_ascii=False))
    return 0


def report_card(
    title: str,
    lines: list[str],
    template: str = "blue",
    daily_progress: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    parsed = {line.split("：", 1)[0]: line.split("：", 1)[1] for line in lines if "：" in line}
    elements: list[dict[str, Any]] = []
    if daily_progress:
        chart = {
            "tag": "chart",
            "chart_spec": {
                "type": "bar",
                "direction": "horizontal",
                "data": {"values": daily_progress},
                "xField": "progress",
                "yField": "metric",
                "seriesField": "metric",
                "color": ["#FA114F", "#92E82A", "#00D9F5"],
                "barWidth": 12,
                "bar": {"style": {"cornerRadius": 6}},
                "label": {"visible": True, "position": "inside-left", "formatter": "{detail}", "style": {"fontSize": 9}},
                "axes": [{"orient": "bottom", "min": 0, "max": 100, "visible": False}, {"orient": "left", "label": {"visible": True}}],
            },
            "height": "88px",
            "preview": False,
        }
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "columns": [{"tag": "column", "width": "weighted", "weight": 1, "elements": [chart]}],
                "margin": "0px 0px 4px 0px",
            }
        )
    recovery = [] if daily_progress else [(label, parsed[label]) for label in ("睡眠", "静息心率") if label in parsed]
    if recovery:
        columns = [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": "grey-50",
                "padding": "8px",
                "vertical_spacing": "2px",
                "elements": [
                    {"tag": "markdown", "content": f"**{value}**", "text_align": "center"},
                    {"tag": "markdown", "content": f"<font color='grey'>{label}</font>", "text_align": "center", "text_size": "notation"},
                ],
            }
            for label, value in recovery
        ]
        elements.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": columns, "margin": "0px 0px 8px 0px"})
    body_detail, body_analysis = parsed.get("身体变化"), parsed.get("身体分析")
    if body_detail:
        content = f"**身体变化**\n{body_detail}"
        if body_analysis:
            content += f"\n<font color='grey'>{body_analysis}</font>"
        elements.append({"tag": "column_set", "flex_mode": "none", "columns": [{"tag": "column", "width": "weighted", "weight": 1, "background_style": "green-50", "padding": "8px", "elements": [{"tag": "markdown", "content": content}]}], "margin": "0px 0px 4px 0px"})
    plan = parsed.get("今日安排") or parsed.get("今日训练")
    training = parsed.get("训练") or parsed.get("昨日运动")
    yesterday = parsed.get("昨日完成")
    if plan or training or yesterday:
        content = "**训练**"
        if yesterday:
            content += f"\n<font color='grey'>昨日：{yesterday}</font>"
        if training:
            content += f"\n{training}"
        if plan:
            content += f"\n今日：{plan}"
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "columns": [{"tag": "column", "width": "weighted", "weight": 1, "background_style": "blue-50", "padding": "8px", "elements": [{"tag": "markdown", "content": content}]}],
                "margin": "0px 0px 4px 0px",
            }
        )
    diet = [(label, parsed[label]) for label in ("饮食热量", "蛋白质", "碳水", "脂肪") if label in parsed]
    if diet:
        columns = [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": "grey-50",
                "padding": "8px",
                "vertical_spacing": "2px",
                "elements": [
                    {"tag": "markdown", "content": f"**{value}**", "text_align": "center"},
                    {"tag": "markdown", "content": f"<font color='grey'>{label}</font>", "text_align": "center", "text_size": "notation"},
                ],
            }
            for label, value in diet
        ]
        elements.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "4px", "columns": columns, "margin": "0px 0px 4px 0px"})
    shown_labels = {"活动热量", "步数", "距离", "睡眠", "静息心率", "心率区间", "身体变化", "身体分析", "训练", "昨日运动", "昨日完成", "今日训练", "今日安排", "饮食热量", "蛋白质", "碳水", "脂肪", "教练建议", "教练分析"}
    remaining = [line for line in lines if line.split("：", 1)[0] not in shown_labels]
    advice = parsed.get("教练建议") or parsed.get("教练分析")
    if advice:
        status = "" if not remaining else "\n" + "\n".join(f"<font color='red'>{line}</font>" for line in remaining)
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "columns": [{"tag": "column", "width": "weighted", "weight": 1, "background_style": "blue-50", "padding": "8px", "elements": [{"tag": "markdown", "content": f"**教练建议**\n{advice}{status}"}]}],
            }
        )
    elif remaining:
        elements.append(
            {
                "tag": "markdown",
                "content": "\n".join(f"<font color='grey'>{line}</font>" for line in remaining),
                "text_size": "notation",
            }
        )
    if not elements:
        elements.append({"tag": "markdown", "content": "\n".join(f"- {line}" for line in lines)})
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "summary": {"content": title}},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": date.today().isoformat()},
            "template": template,
            "icon": {"tag": "standard_icon", "token": "calendar_colorful"},
        },
        "body": {"direction": "vertical", "padding": "8px 12px 8px 12px", "vertical_spacing": "4px", "elements": elements},
    }


def stable_training_templates() -> dict[str, list[str]]:
    return {
        "胸": ["器械推胸3×8–12", "杠铃卧推3×8–10", "上斜推胸3×10–12", "辅助双杠3×8–12", "飞鸟3×12–15", "过头臂屈伸3×10–12"],
        "肩": ["器械推肩3×8–12", "直立划船3×10–12", "反向飞鸟3×12–15", "面拉3×12–15"],
        "背加臂": ["辅助引体3×6–10", "宽距下拉3×10–12", "坐姿划船3×10–12", "弯举3×10–12"],
        "腿": ["倒蹬3×10–12", "腿屈伸3×10–12", "坐姿腿弯举3×10–12", "髋内收3×12–15", "提踵3×12–15"],
    }


SPLIT_ORDER = ("腿", "肩", "胸", "背加臂")


def strength_template_plan(split: str) -> dict[str, Any]:
    templates = stable_training_templates()
    selected = split if split in templates else SPLIT_ORDER[0]
    warmups = {
        "胸": ["肩环绕10次/方向", "弹力带外旋12–15次", "弹力带拉开12–15次", "轻松俯卧撑8–10次×1–2组"],
        "肩": ["肩环绕10次/方向", "墙面滑动10次", "弹力带拉开12–15次", "弹力带外旋12–15次"],
        "背加臂": ["肩胛下沉与后缩10次", "弹力带划船12–15次", "弹力带直臂下压12–15次", "弹力带弯举和臂屈伸各10次"],
        "腿": ["髋膝踝活动各10次", "自重深蹲10次", "臀桥12次", "徒手髋铰链10次"],
    }
    return {
        "name": "背" if selected == "背加臂" else selected,
        "warmup": warmups[selected],
        "items": templates[selected],
        "intensity": "RPE 6–7",
        "cardio": "跑步机或爬楼机30–90分钟",
    }


def initial_week_plan() -> dict[str, dict[str, Any]]:
    return {
        "周四": {"name": "胸背课（全身特训参考）", "items": ["如参加则按课程训练", "不推进力量模板队列"]},
        "周六": {"name": "游泳（有氧参考）", "items": ["如训练可选轻中强度游泳", "不推进力量模板队列"]},
        "周日": {"name": "BODY PUMP（全身特训参考）", "items": ["如参加则按课程训练", "不推进力量模板队列"]},
    }


def _movement_names(train: dict[str, Any]) -> list[str]:
    moves = next((train[key] for key in ("moves", "movements", "actions") if isinstance(train.get(key), list)), [])
    return [str(move[key]).strip() for move in moves if isinstance(move, dict) for key in ("name", "movementName", "title") if move.get(key)]


def _is_fixed_activity(train: dict[str, Any]) -> bool:
    title = next((str(train[key]) for key in ("name", "trainName", "title") if train.get(key)), "")
    normalized = title.upper().replace(" ", "")
    return any(marker in normalized for marker in ("BODYPUMP", "游泳", "胸背课", "胸背训练课"))


def classify_training_split(train: dict[str, Any]) -> str | None:
    if _is_fixed_activity(train):
        return None
    names = _movement_names(train)
    scores: dict[str, int] = {}
    for split, items in stable_training_templates().items():
        bases = [re.split(r"\d+×", item, maxsplit=1)[0] for item in items]
        scores[split] = sum(1 for name in names for base in bases if base in name or name in base)
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    title = next((str(train[key]) for key in ("name", "trainName", "title") if train.get(key)), "")
    if "腿" in title:
        return "腿"
    if "肩" in title:
        return "肩"
    if "胸" in title and "背" not in title:
        return "胸"
    if "背" in title or "臂" in title:
        return "背加臂"
    return None


def completed_split_for_day(xunji: dict[str, Any], day: date) -> str | None:
    for entry in xunji.get("training", []) if isinstance(xunji.get("training"), list) else []:
        if not isinstance(entry, dict):
            continue
        res = entry.get("res") if isinstance(entry.get("res"), dict) else {}
        entry_date = res.get("datestr") or res.get("date") or entry.get("datestr") or entry.get("date")
        if entry_date != day.isoformat():
            continue
        classified = [classify_training_split(train) for train in _day_trains(entry)]
        return next((split for split in classified if split), None)
    return None


def rolling_split_state(xunji: dict[str, Any], today: date, current: str | None, processed_date: str | None) -> dict[str, Any]:
    selected = current if current in SPLIT_ORDER else SPLIT_ORDER[0]
    source_day = today - timedelta(days=1)
    if processed_date == source_day.isoformat():
        return {"recommended": selected, "advanced": False, "source_date": source_day.isoformat()}
    completed = completed_split_for_day(xunji, source_day)
    if not completed:
        return {"recommended": selected, "advanced": False, "source_date": None}
    following = SPLIT_ORDER[(SPLIT_ORDER.index(completed) + 1) % len(SPLIT_ORDER)]
    return {"recommended": following, "advanced": True, "source_date": source_day.isoformat(), "completed": completed}


def today_recommendation(today: date, split: str) -> str:
    weekday = "周" + "一二三四五六日"[today.weekday()]
    fixed = initial_week_plan().get(weekday)
    template = training_plan_brief(strength_template_plan(split), include_warmup=False)
    if fixed:
        return f"{fixed['name']}（可选）；若不参加，可休息或参考滚动模板：{template}"
    return f"滚动模板参考：{template}；可按恢复情况选择训练或休息"


def training_plan_brief(plan: dict[str, Any], include_warmup: bool = True) -> str:
    parts = [str(plan.get("name", "训练"))]
    if include_warmup and plan.get("warmup"):
        parts.append("热身：" + "、".join(str(item) for item in plan["warmup"]))
    parts.extend(str(item) for item in plan.get("items", [])[:3])
    if plan.get("cardio"):
        parts.append("练后有氧：" + str(plan["cardio"]))
    return "；".join(parts)


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_PATH)
    try:
        conn.execute("create table if not exists meta (key text primary key, value text not null)")
        conn.execute("create table if not exists writes (request_id text primary key, status text not null, response text)")
        conn.commit()
        with conn:
            yield conn
    finally:
        conn.close()


def migrate_legacy(legacy_root: Path) -> dict[str, int]:
    counts = {"config": 0, "meta": 0, "writes": 0}
    config_path = legacy_root / "config.json"
    legacy_config: dict[str, Any] | None = None
    if config_path.is_file():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("legacy config must be a JSON object")
        legacy_config = loaded

    legacy_db = legacy_root / "data" / "state.db"
    with db() as destination:
        if legacy_db.is_file():
            with closing(sqlite3.connect(legacy_db)) as source:
                tables = {row[0] for row in source.execute("select name from sqlite_master where type='table'")}
                if "meta" in tables:
                    for key, value in source.execute("select key,value from meta"):
                        if destination.execute("insert or ignore into meta(key,value) values(?,?)", (key, value)).rowcount:
                            counts["meta"] += 1
                if "writes" in tables:
                    for row in source.execute("select request_id,status,response from writes"):
                        if destination.execute("insert or ignore into writes(request_id,status,response) values(?,?,?)", row).rowcount:
                            counts["writes"] += 1
        if legacy_config is not None:
            row = destination.execute("select value from meta where key='config'").fetchone()
            current = json.loads(row[0]) if row else {}
            merged = {**legacy_config, **current}
            encoded = json.dumps(merged, ensure_ascii=False, sort_keys=True)
            if not row or row[0] != encoded:
                destination.execute(
                    "insert into meta(key,value) values('config',?) on conflict(key) do update set value=excluded.value",
                    (encoded,),
                )
                counts["config"] = 1
    return counts


def meta_get(key: str) -> str | None:
    with db() as conn:
        row = conn.execute("select value from meta where key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("insert into meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value", (key, value))


def secret_get(name: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:
        return None


def secret_set(name: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, name, value)


def post_json(url: str, payload: dict[str, Any], secret_name: str, timeout: int = 30) -> dict[str, Any]:
    key = secret_get(secret_name)
    if not key:
        raise RuntimeError(f"missing secret: {secret_name}")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "body-coach/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        safe = exc.read(300).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {safe}") from None


def _tool_json(result: Any) -> dict[str, Any]:
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


async def _mi_query(config: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=config["mi_cli"], args=["serve"])
    result: dict[str, Any] = {}
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            args = {"start_date": start.isoformat(), "end_date": end.isoformat()}
            for name in ("get_daily_summary", "query_sleep", "query_heart_rate", "query_workouts"):
                try:
                    result[name] = _tool_json(await session.call_tool(name, args))
                except Exception as exc:
                    result[name] = {"error": type(exc).__name__}
    return result


def mi_snapshot(config: dict[str, Any], days: int = 7) -> dict[str, Any]:
    if not shutil.which(config["mi_cli"]):
        return {"status": "missing", "error": "mi-fitness-mcp 未安装"}
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    end = date.today()
    start = end - timedelta(days=days)
    sync = subprocess.run([config["mi_cli"], "sync", "--start-date", start.isoformat(), "--end-date", end.isoformat()], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, env=env)
    try:
        data = asyncio.run(_mi_query(config, start, end))
        return {"status": "ok" if sync.returncode == 0 else "partial", "data": data}
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__}


def xunji_snapshot(day: date, weekly: bool = False) -> dict[str, Any]:
    # Keep enough history to recover the latest non-zero nutrition target when
    # Xunji returns zero limits for a day that has standalone food records.
    start = day - timedelta(days=6)
    result: dict[str, Any] = {}
    calls = [
        (
            "diet",
            "https://eatings.xunjiapp.cn/open/food/query_gzip",
            {"start_date": start.isoformat(), "end_date": day.isoformat(), "include_detail": True},
            "xunji_diet_key",
        ),
        (
            "body",
            "https://api.xunjiapp.cn/open/body/query_gzip",
            {"start_date": (day - timedelta(days=30)).isoformat(), "end_date": day.isoformat(), "include_latest": True, "include_records": True, "limit": 100, "offset": 0},
            "xunji_body_key",
        ),
    ]
    for name, url, payload, key in calls:
        try:
            result[name] = post_json(url, payload, key)
        except Exception as exc:
            result[name] = {"error": str(exc)}
    training = []
    for offset in range(7 if weekly else 2):
        target = day - timedelta(days=offset)
        try:
            training.append(
                post_json(
                    "https://trains.xunjiapp.cn/api_trains_for_llm_v2",
                    {"schema_version": "train_open_api_v2", "datestr": target.isoformat(), "include_full_data": False},
                    "xunji_train_key",
                )
            )
        except Exception as exc:
            training.append({"date": target.isoformat(), "error": str(exc)})
    result["training"] = training
    return result


def _walk_value(data: Any, names: set[str]) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in names and not isinstance(value, (dict, list)):
                return value
        for value in data.values():
            found = _walk_value(value, names)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _walk_value(value, names)
            if found is not None:
                return found
    return None


def compact_data(data: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(data, dict):
        return {k: compact_data(v, depth + 1) for k, v in list(data.items())[:20] if "token" not in k.lower() and "key" not in k.lower()}
    if isinstance(data, list):
        return [compact_data(v, depth + 1) for v in data[:8]]
    if isinstance(data, str):
        return data[:300]
    return data


def mi_cache_overview(day: date | None = None) -> dict[str, Any]:
    try:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        database_path = local_app_data / "mi-fitness-mcp/mi-fitness-mcp/mi_fitness.db"
        with sqlite3.connect(database_path) as conn:
            conn.row_factory = sqlite3.Row
            if day is None:
                activity = conn.execute("select date,steps,distance_m,active_kcal,active_minutes from daily_activity order by date desc limit 1").fetchone()
                sleep = conn.execute("select start_at,end_at,duration_minutes,time_asleep_minutes,sleep_score from sleep_sessions order by end_at desc limit 1").fetchone()
                heart = conn.execute(
                    "select substr(timestamp,1,10) date,min(bpm) min_bpm,max(bpm) max_bpm,"
                    "max(case when lower(sample_type) like '%rest%' then bpm end) resting_bpm "
                    "from heart_rate_samples group by substr(timestamp,1,10) order by date desc limit 1"
                ).fetchone()
            else:
                day_text = day.isoformat()
                next_day_text = (day + timedelta(days=1)).isoformat()
                activity = conn.execute(
                    "select date,steps,distance_m,active_kcal,active_minutes from daily_activity where date=? limit 1",
                    (day_text,),
                ).fetchone()
                sleep = conn.execute(
                    "select start_at,end_at,duration_minutes,time_asleep_minutes,sleep_score from sleep_sessions "
                    "where substr(end_at,1,10) in (?,?) "
                    "order by coalesce(time_asleep_minutes,duration_minutes) desc,end_at desc limit 1",
                    (day_text, next_day_text),
                ).fetchone()
                heart = conn.execute(
                    "select substr(timestamp,1,10) date,min(bpm) min_bpm,max(bpm) max_bpm,"
                    "max(case when lower(sample_type) like '%rest%' then bpm end) resting_bpm "
                    "from heart_rate_samples where substr(timestamp,1,10)=? group by substr(timestamp,1,10)",
                    (day_text,),
                ).fetchone()
            body = conn.execute(
                "select timestamp,weight_kg,bmi,body_fat_pct,basal_metabolism_kcal "
                "from body_measurements order by timestamp desc limit 1"
            ).fetchone()
        return {
            "activity": dict(activity) if activity else {},
            "sleep": dict(sleep) if sleep else {},
            "heart": dict(heart) if heart else {},
            "body": dict(body) if body else {},
        }
    except Exception:
        return {"activity": {}, "sleep": {}, "heart": {}, "body": {}}


def mi_body_history(limit: int = 2) -> list[dict[str, Any]]:
    try:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        database_path = local_app_data / "mi-fitness-mcp/mi-fitness-mcp/mi_fitness.db"
        with sqlite3.connect(database_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select timestamp,weight_kg,bmi,body_fat_pct,muscle_mass_kg,basal_metabolism_kcal "
                "from body_measurements order by timestamp desc limit ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def body_change_summary(records: list[dict[str, Any]], last_reported: str | None) -> dict[str, str] | None:
    if not records or records[0].get("timestamp") == last_reported:
        return None

    def normalized(record: dict[str, Any]) -> dict[str, Any]:
        values = dict(record)
        weight, muscle = values.get("weight_kg"), values.get("muscle_mass_kg")
        if isinstance(weight, (int, float)) and isinstance(muscle, (int, float)) and muscle > weight:
            values["muscle_rate_pct"] = muscle
            values["muscle_mass_kg"] = weight * muscle / 100
        elif isinstance(weight, (int, float)) and weight and isinstance(muscle, (int, float)):
            values["muscle_rate_pct"] = muscle / weight * 100
        return values

    latest = normalized(records[0])
    previous = normalized(records[1]) if len(records) > 1 else None
    timestamp = str(latest.get("timestamp", ""))
    measured = _record_date(timestamp)
    detail = [measured.isoformat() if measured else timestamp[:10]]
    fields = (("体重", "weight_kg", "kg"), ("BMI", "bmi", ""), ("体脂", "body_fat_pct", "%"))
    detail.extend(f"{label}{latest[key]:.1f}{unit}" for label, key, unit in fields if isinstance(latest.get(key), (int, float)))
    if isinstance(latest.get("muscle_mass_kg"), (int, float)):
        muscle = f"肌肉{latest['muscle_mass_kg']:.1f}kg"
        if isinstance(latest.get("muscle_rate_pct"), (int, float)):
            muscle += f" / 肌肉率{latest['muscle_rate_pct']:.1f}%"
        detail.append(muscle)
    if isinstance(latest.get("basal_metabolism_kcal"), (int, float)):
        detail.append(f"基础代谢{latest['basal_metabolism_kcal']:.0f}kcal")
    if not previous:
        analysis = "首次记录，暂无历史对比"
    else:
        deltas = []
        for label, key, unit in (("体重", "weight_kg", "kg"), ("BMI", "bmi", ""), ("体脂", "body_fat_pct", "个百分点"), ("肌肉", "muscle_mass_kg", "kg")):
            if isinstance(latest.get(key), (int, float)) and isinstance(previous.get(key), (int, float)):
                deltas.append(f"{label}{latest[key] - previous[key]:+.1f}{unit}")
        analysis = "较上次：" + "、".join(deltas) + "；单次波动仅供参考"
    return {"timestamp": timestamp, "detail": " · ".join(detail), "analysis": analysis}


def body_aware_advice(advice: str, body_change: dict[str, str] | None) -> str:
    if not body_change:
        return advice
    return f"身体评价：{body_change['analysis']}；{advice}"


def _record_date(value: Any) -> date | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().date() if parsed.tzinfo else parsed.date()
    except (TypeError, ValueError):
        return None


def _clock(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone()
        return parsed.strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


MI_CARDIO = {
    "running": ("跑步", "running", "跑步"),
    "outdoor_running": ("跑步", "running", "跑步"),
    "swimming": ("游泳", "swimming", "游泳"),
    "pool_swimming": ("游泳", "swimming", "游泳"),
    "open_water_swimming": ("游泳", "swimming", "游泳"),
    "walking": ("快走", "walking", "快走"),
    "outdoor_walking": ("快走", "walking", "快走"),
    "hiking": ("快走", "walking", "徒步"),
    "outdoor_hiking": ("快走", "walking", "徒步"),
    "cycling": ("动感单车（有氧）", "cycling", "骑行"),
    "outdoor_cycling": ("动感单车（有氧）", "cycling", "骑行"),
    "elliptical": ("椭圆机", "general", "椭圆机"),
    "rowing": ("划船机", "general", "划船机"),
    "jump_rope": ("跳绳", "jumpRope", "跳绳"),
}


def mi_workouts_for_day(mi: dict[str, Any], day: date) -> list[dict[str, Any]]:
    workouts = mi.get("data", {}).get("query_workouts", {}).get("data", {}).get("workouts", [])
    return [
        workout for workout in workouts
        if isinstance(workout, dict)
        and workout.get("workout_id")
        and workout.get("activity_type") in MI_CARDIO
        and _record_date(workout.get("start_at")) == day
    ]


def workout_brief(mi: dict[str, Any], day: date, free_label: str = "自由活动") -> str:
    parts = []
    workouts = mi.get("data", {}).get("query_workouts", {}).get("data", {}).get("workouts", [])
    for workout in workouts:
        if not isinstance(workout, dict) or _record_date(workout.get("start_at")) != day:
            continue
        activity_type = workout.get("activity_type")
        if activity_type not in MI_CARDIO and activity_type != "free_training":
            continue
        label = MI_CARDIO[activity_type][2] if activity_type in MI_CARDIO else free_label
        details = []
        if isinstance(workout.get("duration_minutes"), (int, float)):
            details.append(f"{workout['duration_minutes']:.0f}分钟")
        if isinstance(workout.get("distance_m"), (int, float)):
            details.append(f"{workout['distance_m'] / 1000:.1f}km")
        if isinstance(workout.get("calories_kcal"), (int, float)):
            details.append(f"{workout['calories_kcal']:.0f}kcal")
        average, maximum = workout.get("avg_heart_rate_bpm"), workout.get("max_heart_rate_bpm")
        if isinstance(average, (int, float)):
            details.append(f"心率{average:.0f}/{maximum:.0f}" if isinstance(maximum, (int, float)) else f"均心{average:.0f}")
        parts.append(label + " " + (" · ".join(details) if details else "数据不完整"))
    return "；".join(parts) if parts else "暂无"


def _epoch_ms(value: str) -> int:
    return round(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _pace(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def xunji_cardio_record(workout: dict[str, Any], day: date) -> dict[str, Any]:
    name, preset, label = MI_CARDIO[workout["activity_type"]]
    metrics: dict[str, str] = {"workoutTime": str(round(workout["duration_minutes"] * 60))}
    optional = {
        "distance": f"{workout['distance_m'] / 1000:.2f}" if isinstance(workout.get("distance_m"), (int, float)) else None,
        "calories": f"{workout['calories_kcal']:.0f}" if isinstance(workout.get("calories_kcal"), (int, float)) else None,
        "avgHeartRate": f"{workout['avg_heart_rate_bpm']:.0f}" if isinstance(workout.get("avg_heart_rate_bpm"), (int, float)) else None,
        "maxHeartRate": f"{workout['max_heart_rate_bpm']:.0f}" if isinstance(workout.get("max_heart_rate_bpm"), (int, float)) else None,
        "pace": _pace(workout.get("avg_pace_sec_per_km")),
    }
    metrics.update({key: value for key, value in optional.items() if value is not None})
    return {
        "datestr": day.isoformat(),
        "title": f"小米运动 {label}",
        "start": _epoch_ms(workout["start_at"]),
        "end": _epoch_ms(workout["end_at"]),
        "movements": [{"name": name, "cardio": True, "recordPreset": preset, "metrics": metrics}],
    }


def sync_mi_workouts(mi: dict[str, Any], day: date) -> str:
    since = meta_get("mi_workout_sync_since")
    if not since or day < date.fromisoformat(since):
        return "not_enabled_for_day"
    workouts = [
        workout for workout in mi_workouts_for_day(mi, day)
        if _record_date(workout.get("end_at")) is not None
        and isinstance(workout.get("duration_minutes"), (int, float))
        and workout["duration_minutes"] > 0
    ]
    if not workouts:
        return "none"
    if len(workouts) > 4:
        return "too_many"
    records = [xunji_cardio_record(workout, day) for workout in workouts]
    rid = request_id("cardio", day.isoformat(), [workout["workout_id"] for workout in workouts])
    with db() as conn:
        if conn.execute("select 1 from writes where request_id=? and status='ok'", (rid,)).fetchone():
            return "already_synced"
    result = post_json(
        "https://trains.xunjiapp.cn/api_upsert_trains_for_llm_v2",
        {"schema_version": "train_open_api_v2", "client_request_id": rid, "dry_run": False, "include_full_data": False, "res": records},
        "xunji_train_key",
    )
    if result.get("success") is False:
        raise RuntimeError(f"训记运动写入失败：{result.get('res', 'unknown')}")
    with db() as conn:
        conn.execute("insert or replace into writes(request_id,status,response) values(?,?,?)", (rid, "ok", json.dumps(compact_data(result), ensure_ascii=False)))
    return f"written:{len(records)}"


def daily_progress(overview: dict[str, Any], day: date | None = None) -> list[dict[str, Any]]:
    day = day or date.today()
    activity = overview.get("activity", {}) if _record_date(overview.get("activity", {}).get("date")) == day else {}
    sleep_date = _record_date(overview.get("sleep", {}).get("end_at"))
    sleep = overview.get("sleep", {}) if sleep_date in (day, day + timedelta(days=1)) else {}
    heart = overview.get("heart", {}) if _record_date(overview.get("heart", {}).get("date")) == day else {}
    steps = activity.get("steps")
    asleep = sleep.get("time_asleep_minutes") or sleep.get("duration_minutes")
    sleep_start, sleep_end = _clock(sleep.get("start_at")), _clock(sleep.get("end_at"))
    sleep_detail = "睡眠今日暂无"
    if isinstance(asleep, (int, float)):
        interval = f"{sleep_start}–{sleep_end} · " if sleep_start and sleep_end else ""
        sleep_detail = f"{interval}{asleep / 60:.1f}/8.0小时"
    minimum, maximum, resting = heart.get("min_bpm"), heart.get("max_bpm"), heart.get("resting_bpm")
    heart_detail = "心率今日暂无"
    if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
        heart_detail = f"{minimum:.0f}–{maximum:.0f} bpm · 静息{resting:.0f}" if isinstance(resting, (int, float)) else f"{minimum:.0f}–{maximum:.0f} bpm · 静息暂无"
    return [
        {"metric": "步数", "progress": 0 if not isinstance(steps, (int, float)) else round(min(max(steps / 8000 * 100, 0), 100), 1), "detail": "步数今日暂无" if not isinstance(steps, (int, float)) else f"{steps:.0f}/8000 步"},
        {"metric": "睡眠", "progress": 0 if not isinstance(asleep, (int, float)) else round(min(max(asleep / 480 * 100, 0), 100), 1), "detail": sleep_detail},
        {"metric": "心率", "progress": 0 if not isinstance(maximum, (int, float)) else round(min(max(maximum / 200 * 100, 0), 100), 1), "detail": heart_detail},
    ]


def nutrition_targets(overview: dict[str, Any], body_override: dict[str, Any] | None = None) -> dict[str, int]:
    body = body_override or overview.get("body", {})
    weight = body.get("weight_kg")
    basal = body.get("basal_metabolism_kcal")
    if not isinstance(weight, (int, float)) or not 30 <= weight <= 300 or not isinstance(basal, (int, float)) or not 800 <= basal <= 4000:
        return {}
    calories = round(basal * 1.45 / 50) * 50
    protein = round(weight * 1.8 / 5) * 5
    fat = round(calories * 0.27 / 9 / 5) * 5
    carbs = round((calories - protein * 4 - fat * 9) / 4 / 5) * 5
    return {"calories": calories, "protein": protein, "carbs": carbs, "fat": fat}


def xunji_nutrition_targets(diet: dict[str, Any]) -> dict[str, int]:
    days = diet.get("res", {}).get("days", []) if isinstance(diet, dict) else []
    for item in reversed(days if isinstance(days, list) else []):
        limits = item.get("foods", {}).get("limits", {}) if isinstance(item, dict) else {}
        try:
            protein, carbs, fat = (round(float(limits[key])) for key in ("protein", "carb", "fat"))
        except (KeyError, TypeError, ValueError):
            continue
        if protein > 0 and carbs > 0 and fat > 0:
            return {"calories": protein * 4 + carbs * 4 + fat * 9, "protein": protein, "carbs": carbs, "fat": fat}
    return {}


def xunji_diet_day(diet: dict[str, Any], day: date) -> dict[str, Any]:
    days = diet.get("res", {}).get("days", []) if isinstance(diet, dict) else []
    return next((item for item in days if isinstance(item, dict) and (item.get("date") or item.get("datestr")) == day.isoformat()), {})


def missing_meals(diet: dict[str, Any], day: date) -> list[str]:
    records = xunji_diet_day(diet, day).get("foods", {}).get("records", [])
    seen = {str(item.get("meal_type", "")).strip().lower() for item in records if isinstance(item, dict)}
    aliases = {
        "早餐": {"morning", "breakfast", "早餐"},
        "午餐": {"noon", "midday", "lunch", "午餐"},
        "晚餐": {"evening", "night", "dinner", "晚餐"},
    }
    return [label for label, values in aliases.items() if not seen.intersection(values)]


def _food_totals(foods: list[dict[str, Any]]) -> dict[str, float]:
    totals = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
    keys = {"calories": "cal", "protein": "protein", "carbs": "carb", "fat": "fat"}
    for food in foods:
        amount, unit, ntr = food.get("amount"), str(food.get("unit", "")).lower(), food.get("ntr", {})
        grams = float(amount) if isinstance(amount, (int, float)) and unit in ("g", "克", "ml", "毫升") else None
        if grams is None and isinstance(amount, (int, float)):
            match = next((item for item in ntr.get("foodUnit", []) if str(item.get("unit", "")).lower() == unit and isinstance(item.get("gram"), (int, float))), None)
            grams = float(amount) * float(match["gram"]) if match else None
        if grams is None:
            continue
        for target_key, ntr_key in keys.items():
            if isinstance(ntr.get(ntr_key), (int, float)):
                totals[target_key] += float(ntr[ntr_key]) * grams / 100
    return {key: round(value, 1) for key, value in totals.items()}


def meal_feedback(diet: dict[str, Any], day: date, foods: list[dict[str, Any]]) -> dict[str, Any]:
    today = xunji_diet_day(diet, day)
    raw = today.get("totals", {})
    server_totals = {
        "calories": float(raw.get("totalCal", 0) or 0),
        "protein": float(raw.get("totalProtein", 0) or 0),
        "carbs": float(raw.get("totalCarb", 0) or 0),
        "fat": float(raw.get("totalFat", 0) or 0),
    }
    records = today.get("foods", {}).get("records", [])
    recalculated = _food_totals(records) if isinstance(records, list) else {}
    consumed = recalculated if any(recalculated.values()) else server_totals
    targets = xunji_nutrition_targets(diet)
    remaining = {key: round(max(float(targets[key]) - value, 0), 1) for key, value in consumed.items()} if targets else {}
    recent = []
    for item in diet.get("res", {}).get("days", []):
        totals = item.get("totals", {}) if isinstance(item, dict) else {}
        if (item.get("date") or item.get("datestr")) != day.isoformat() and isinstance(totals.get("totalCal"), (int, float)):
            recent.append(totals)
    recent_note = "近期记录不足"
    if recent and targets:
        avg_cal = sum(float(item.get("totalCal", 0) or 0) for item in recent) / len(recent)
        avg_protein = sum(float(item.get("totalProtein", 0) or 0) for item in recent) / len(recent)
        if avg_protein < targets["protein"] * 0.8:
            recent_note = "近7日蛋白质偏低"
        elif avg_cal > targets["calories"] * 1.1:
            recent_note = "近7日总热量偏高"
        elif avg_cal < targets["calories"] * 0.7:
            recent_note = "近7日总热量偏低"
        else:
            recent_note = "近7日摄入较稳定"
    suggestions = []
    if remaining.get("protein", 0) >= 30:
        suggestions.append("补30–40g优质蛋白")
    suggestions.append("搭配适量主食" if remaining.get("carbs", 0) >= 50 else "主食少量")
    if remaining.get("fat", 0) <= 15:
        suggestions.append("少油少酱")
    suggestions.append("加一份蔬菜")
    meal = _food_totals(foods)
    if not targets:
        evaluation = "本餐已记录；训记暂无饮食目标，暂不判断是否超标"
    elif meal["fat"] >= targets["fat"] * 0.4:
        evaluation = "本餐脂肪偏高；下一餐优先低脂蛋白、少油少酱"
    elif meal["calories"] >= 300 and meal["protein"] < 20:
        evaluation = "本餐蛋白质偏少；下一餐增加一份优质蛋白"
    else:
        evaluation = "本餐摄入基本合理；继续按今日剩余额度安排"
    return {
        "meal": meal,
        "consumed": {key: round(value, 1) for key, value in consumed.items()},
        "targets": targets,
        "remaining": remaining,
        "recent": recent_note,
        "evaluation": evaluation,
        "next_meal": "；".join(suggestions),
    }


def _has_error(data: Any) -> bool:
    if isinstance(data, dict):
        return "error" in data or any(_has_error(value) for value in data.values())
    if isinstance(data, list):
        return any(_has_error(value) for value in data)
    return False


def _day_trains(day: Any) -> list[dict[str, Any]]:
    if not isinstance(day, dict) or not isinstance(day.get("res"), dict) or not isinstance(day["res"].get("trains"), list):
        return []
    return [train for train in day["res"]["trains"] if isinstance(train, dict)]


def training_brief(xunji: dict[str, Any], day_index: int = 0) -> str:
    days = xunji.get("training", [])
    selected = days[day_index:day_index + 1] if isinstance(days, list) else []
    trains = [train for day in selected for train in _day_trains(day)]
    if not trains:
        return "今日暂无训练记录"
    train = trains[0]
    name = next((str(train[key]) for key in ("name", "trainName", "title") if train.get(key)), "训练")
    moves = next((train[key] for key in ("moves", "movements", "actions") if isinstance(train.get(key), list)), [])
    move_names = [str(move[key]) for move in moves for key in ("name", "movementName", "title") if isinstance(move, dict) and move.get(key)][:4]
    set_count = sum(len(move.get("sets", [])) for move in moves if isinstance(move, dict) and isinstance(move.get("sets"), list))
    detail = "、".join(move_names)
    return " · ".join(part for part in (name, detail, f"{set_count} 组" if set_count else "") if part)


def yesterday_completion(xunji: dict[str, Any]) -> str:
    days = xunji.get("training", [])
    if not isinstance(days, list) or len(days) < 2:
        return "无记录"
    trains = _day_trains(days[1])
    if not trains:
        return "无训练记录"
    moves = [move for train in trains for move in next((train[key] for key in ("moves", "movements", "actions") if isinstance(train.get(key), list)), []) if isinstance(move, dict)]
    names = [str(move[key]) for move in moves for key in ("name", "movementName", "title") if move.get(key)][:4]
    projects = "、".join(names) or next((str(train[key]) for train in trains for key in ("name", "trainName", "title") if train.get(key)), "训练")
    sets = [item for move in moves for item in move.get("sets", []) if isinstance(move.get("sets"), list) and isinstance(item, dict)]
    if not sets:
        return f"{projects} · 已记录 {len(trains)} 项训练"
    markers = ("done", "completed", "isDone", "is_completed")
    marked = [next(bool(item[key]) for key in markers if key in item) for item in sets if any(key in item for key in markers)]
    if not marked:
        return f"{projects} · 已记录 {len(sets)} 组"
    completed = sum(marked)
    return f"{projects} · {completed}/{len(sets)} 组（{round(completed / len(sets) * 100)}%）"


def local_summary(mi: dict[str, Any], xunji: dict[str, Any], today: date, recommended_split: str = "腿") -> list[str]:
    summary_day = today - timedelta(days=1)
    cache = mi_cache_overview(summary_day)
    activity = cache["activity"] if _record_date(cache["activity"].get("date")) == summary_day else {}
    sleep_date = _record_date(cache["sleep"].get("end_at"))
    sleep_data = cache["sleep"] if sleep_date in (summary_day, today) else {}
    heart = cache["heart"] if _record_date(cache["heart"].get("date")) == summary_day else {}
    steps = activity.get("steps")
    active_kcal = activity.get("active_kcal")
    distance = activity.get("distance_m")
    sleep = sleep_data.get("time_asleep_minutes") or sleep_data.get("duration_minutes")
    resting = heart.get("resting_bpm")
    diet = xunji.get("diet", {})
    totals = xunji_diet_day(diet, summary_day).get("totals", {})
    calories = totals.get("totalCal")
    protein = totals.get("totalProtein")
    carbs = totals.get("totalCarb")
    fat = totals.get("totalFat")
    targets = xunji_nutrition_targets(diet)

    def nutrition_line(label: str, value: Any, target: str, unit: str) -> str:
        actual = f"{value:.0f}" if isinstance(value, (int, float)) else "暂无"
        goal = f" / {targets[target]}" if targets else ""
        return f"{label}：{actual}{goal} {unit}"
    summary_plan = initial_week_plan().get("周" + "一二三四五六日"[summary_day.weekday()])
    completion = yesterday_completion(xunji)
    # ponytail: day-level association; use time overlap if Xunji exposes reliable session times.
    free_label = "力量训练手环数据" if completion not in ("无记录", "无训练记录") else summary_plan["name"] if summary_plan else "自由活动"
    today_training = training_brief(xunji)
    if today_training == "今日暂无训练记录":
        today_training = today_recommendation(today, recommended_split)
    lines = [
        f"活动热量：{active_kcal:.0f} kcal" if isinstance(active_kcal, (int, float)) else "活动热量：暂无",
        f"步数：{steps if steps is not None else '暂无'} 步" if steps is not None else "步数：暂无",
        f"距离：{distance / 1000:.1f} km" if isinstance(distance, (int, float)) else "距离：暂无",
        f"睡眠：{sleep / 60:.1f} 小时" if isinstance(sleep, (int, float)) else "睡眠：暂无",
        f"静息心率：{resting} bpm" if resting is not None else "静息心率：今日暂无",
        f"昨日运动：{workout_brief(mi, summary_day, free_label)}",
        f"昨日完成：{completion}",
        f"今日训练：{today_training}",
        nutrition_line("饮食热量", calories, "calories", "kcal"),
        nutrition_line("蛋白质", protein, "protein", "g"),
        nutrition_line("碳水", carbs, "carbs", "g"),
        nutrition_line("脂肪", fat, "fat", "g"),
    ]
    if mi.get("status") != "ok":
        lines.append(f"小米数据：{mi.get('status', 'unknown')}")
    if _has_error(xunji):
        lines.append("训记数据：部分读取异常")
    return lines


def advice_prompt(mi: dict[str, Any], xunji: dict[str, Any], weekly: bool = False, context: list[str] | None = None) -> str:
    payload = {"summary": context or [], "mi": compact_data(mi), "xunji": compact_data(xunji), "weekly": weekly}
    if weekly:
        format_rule = (
            "结合一周趋势，用恢复、训练、营养三个标签各写1至2句，总计不超过360个汉字；"
            "不用Markdown标题或列表。指出主要变化、下周行动和依据，"
            "避免重复罗列原始数据。最多提出一项模板调整，必须写明理由并标注待用户确认，不得表述为已修改。"
        )
    else:
        format_rule = (
            "结合昨日状态和今日安排，只给1至2句、不超过120个汉字的行动建议。"
            "必须先综合权衡身体变化、睡眠与恢复、活动、训练完成度和饮食数据，再保留最重要的1至2个结论；"
            "不设分项标题，不机械复述summary中的数据和今日训练内容。"
            "训练动作必须沿用今日参考模板，不得新增或替换训练动作；确需调整时只建议减量或休息。"
        )
    return (
        "请以专业健身教练和营养师的综合视角，根据下面聚合数据给出中文建议，不做医疗诊断。"
        + format_rule +
        "数据缺失时不得臆测；出现持续疼痛、肿胀、麻木、无力或胸部不适时建议寻求专业帮助。"
        "summary 是已校验的日报事实，不得与其中的今日训练安排冲突。"
        "不要输出身心健康或情绪压力项目。\n" + json.dumps(payload, ensure_ascii=False)
    )


DAILY_FALLBACK_ADVICE = "按今日模板量力训练；优先补足睡眠、蛋白质和饮水，疲劳明显则减量或休息。"
WEEKLY_FALLBACK_ADVICE = "恢复：关注睡眠与疲劳趋势；训练：按完成度逐步加量；营养：围绕训练日补足蛋白质、碳水和饮水。"


def command_available(value: str | None) -> bool:
    return bool(value and (shutil.which(value) or Path(value).is_file()))


def safe_error_detail(value: str) -> str:
    value = TOKEN_RE.sub("[redacted]", value)
    value = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", value, flags=re.IGNORECASE)
    return " ".join(value.split())[:500]


def model_diagnostic(config: dict[str, Any]) -> dict[str, Any]:
    executable = command_available(config.get("codex_cli"))
    if not config.get("model_analysis"):
        return {"available": False, "enabled": False, "executable": executable, "reason": "disabled"}
    if not executable:
        return {"available": False, "enabled": True, "executable": False, "reason": "missing_cli"}
    raw = meta_get("last_model_error")
    if raw:
        try:
            saved = json.loads(raw)
            if isinstance(saved, dict) and saved.get("reason"):
                return {"available": False, "enabled": True, "executable": True, **saved}
        except json.JSONDecodeError:
            pass
    return {"available": True, "enabled": True, "executable": True, "reason": "ready"}


def record_model_error(reason: str, *, exit_code: int | None = None, output_present: bool = False, detail: str = "") -> None:
    payload: dict[str, Any] = {"reason": reason, "output_present": output_present}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if detail:
        payload["detail"] = safe_error_detail(detail)
    meta_set("last_model_error", json.dumps(payload, ensure_ascii=False))


def trim_advice(value: str, limit: int) -> str:
    value = re.sub(r"(?m)^#{1,6}\s*", "", value)
    value = " ".join(line.strip(" -*") for line in value.splitlines() if line.strip(" -*"))
    if len(value) <= limit:
        return value
    clipped = value[:limit]
    cut = max(clipped.rfind(mark) for mark in "。！？；")
    return clipped[:cut + 1] if cut >= limit // 2 else clipped.rstrip()


def model_advice(config: dict[str, Any], mi: dict[str, Any], xunji: dict[str, Any], weekly: bool = False, context: list[str] | None = None) -> str:
    fallback = WEEKLY_FALLBACK_ADVICE if weekly else DAILY_FALLBACK_ADVICE
    if not config.get("model_analysis") or not command_available(config.get("codex_cli")):
        return fallback
    prompt = advice_prompt(mi, xunji, weekly, context)
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "answer.txt"
        command = command_prefix(config["codex_cli"]) + [
            "exec", "--ephemeral", "--sandbox", "read-only",
            "--skip-git-repo-check", "--ignore-rules", "-C", str(ROOT), "-o", str(output), "-",
        ]
        try:
            completed = subprocess.run(command, input=prompt, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=120)
            if completed.returncode == 0 and output.exists():
                answer = output.read_text(encoding="utf-8").strip()
                if answer:
                    meta_set("last_model_error", "")
                    return trim_advice(answer, 360 if weekly else 120)
            record_model_error("process_failed", exit_code=completed.returncode, output_present=output.exists(), detail=completed.stderr)
        except subprocess.TimeoutExpired:
            record_model_error("timeout")
        except Exception as exc:
            record_model_error("exception", detail=type(exc).__name__)
    return "模型分析暂不可用（运行 doctor 查看原因）；" + fallback


def send_report(config: dict[str, Any], title: str, lines: list[str], identity: str, progress: list[dict[str, Any]] | None = None) -> bool:
    ensure_lark_channel_env()
    card = json.dumps(report_card(title, lines, daily_progress=progress), ensure_ascii=False, separators=(",", ":"))
    base = command_prefix(config["lark_cli"]) + ["im", "+messages-send", "--as", "bot", "--chat-id", config["chat_id"], "--idempotency-key", identity]
    sent = subprocess.run(base + ["--msg-type", "interactive", "--content", card], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if sent.returncode == 0:
        return True
    text = title + "\n" + "\n".join(f"- {line}" for line in lines)
    fallback = subprocess.run(base + ["--text", text], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    return fallback.returncode == 0


def preview_report(title: str, lines: list[str], progress: list[dict[str, Any]] | None = None) -> None:
    print(json.dumps({"dry_run": True, "title": title, "lines": lines, "progress": progress or []}, ensure_ascii=False, indent=2))


def run_daily(force: bool = False, dry_run: bool = False) -> int:
    config = load_config()
    today = date.today()
    last_raw = meta_get("last_daily")
    last = date.fromisoformat(last_raw) if last_raw else None
    if last == today and not force:
        return 0
    missed = missing_dates(last, today)
    mi = mi_snapshot(config)
    xunji = xunji_snapshot(today)
    split_state = rolling_split_state(xunji, today, meta_get("next_strength_template"), meta_get("last_split_training_date"))
    sync_status = "dry_run" if dry_run else "none"
    if not dry_run:
        try:
            sync_status = sync_mi_workouts(mi, today - timedelta(days=1))
        except Exception as exc:
            sync_status = f"error:{type(exc).__name__}"
    if missed and not dry_run:
        send_report(config, "补发日报", [f"电脑关闭期间缺少 {len(missed)} 天日报", f"日期：{missed[0]} 至 {missed[-1]}", "数据已在本次启动后重新同步"], request_id("catchup", today.isoformat(), [str(d) for d in missed]))
    lines = local_summary(mi, xunji, today, split_state["recommended"])
    body_change = body_change_summary(mi_body_history(), meta_get("last_body_measurement"))
    if body_change:
        lines.extend(["身体变化：" + body_change["detail"], "身体分析：" + body_change["analysis"]])
    if sync_status.startswith("error") or sync_status == "too_many":
        lines.append("小米运动写入训记：失败，已保留原始记录")
    advice = model_advice(config, mi, xunji, context=lines)
    lines.append("教练建议：" + advice)
    progress = daily_progress(mi_cache_overview(today - timedelta(days=1)), today - timedelta(days=1))
    if dry_run:
        preview_report("昨日总结 · 今日安排", lines, progress)
        return 0
    if not send_report(config, "昨日总结 · 今日安排", lines, request_id("daily", today.isoformat(), lines), progress):
        return 2
    meta_set("last_daily", today.isoformat())
    if split_state["advanced"]:
        meta_set("next_strength_template", split_state["recommended"])
        meta_set("last_split_training_date", split_state["source_date"])
    if body_change:
        meta_set("last_body_measurement", body_change["timestamp"])
    if today.weekday() == 0 and meta_get("last_weekly") != today.isoformat():
        run_weekly()
    return 0


def run_weekly(dry_run: bool = False) -> int:
    config = load_config()
    today = date.today()
    mi = mi_snapshot(config, 14)
    xunji = xunji_snapshot(today, weekly=True)
    lines = [
        "周期：最近7天",
        "训练、睡眠、步数与饮食按趋势判断，不评价单日波动",
        "模板更新：周报仅提出建议；展示具体差异并经你确认后才会修改",
        "教练分析：" + model_advice(config, mi, xunji, weekly=True),
    ]
    if dry_run:
        preview_report("健身周报", lines)
        return 0
    if not send_report(config, "健身周报", lines, request_id("weekly", today.isoformat(), lines)):
        return 2
    meta_set("last_weekly", today.isoformat())
    return 0


def run_meal_reminder(dry_run: bool = False) -> int:
    config = load_config()
    today = date.today()
    try:
        diet = post_json(
            "https://eatings.xunjiapp.cn/open/food/query_gzip",
            {"start_date": today.isoformat(), "end_date": today.isoformat(), "include_detail": True},
            "xunji_diet_key",
        )
    except Exception:
        return 2
    missing = missing_meals(diet, today)
    if not missing:
        return 0
    lines = [f"未记录：{'、'.join(missing)}", "如已用餐，请把食物和大致份量发给我；未用餐可忽略。"]
    if dry_run:
        preview_report("三餐记录提醒", lines)
        return 0
    return 0 if send_report(config, "三餐记录提醒", lines, request_id("meal", today.isoformat(), missing)) else 2


def write_food(path: Path, source_id: str) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    foods = normalize_food_units(payload.get("foods", []))
    if not foods or not all(diet_auto_allowed(food) and food.get("ntr") for food in foods):
        print("需要明确餐次、食物、数量、单位和营养估算；未写入。")
        return 3
    rid = request_id("diet", source_id, foods)
    with db() as conn:
        if conn.execute("select 1 from writes where request_id=? and status='ok'", (rid,)).fetchone():
            print("已处理，未重复写入。")
            return 0
    result = post_json("https://eatings.xunjiapp.cn/open/food/upsert_gzip", {"client_request_id": rid, "dry_run": False, "foods": foods}, "xunji_diet_key")
    with db() as conn:
        conn.execute("insert or replace into writes(request_id,status,response) values(?,?,?)", (rid, "ok", json.dumps(compact_data(result), ensure_ascii=False)))
    day = date.fromisoformat(foods[0].get("date", date.today().isoformat()))
    try:
        diet = post_json("https://eatings.xunjiapp.cn/open/food/query_gzip", {"start_date": (day - timedelta(days=6)).isoformat(), "end_date": day.isoformat(), "include_detail": True}, "xunji_diet_key")
    except Exception:
        written_day = result.get("res", {}).get("day", {})
        diet = {"res": {"days": [written_day] if written_day else []}}
    print(json.dumps({"request_id": rid, "written": len(foods), "feedback": meal_feedback(diet, day, foods)}, ensure_ascii=False))
    return 0


def doctor() -> int:
    config = load_config()
    commands = {name: bool(shutil.which(value) or Path(value).exists()) for name, value in (("mi", config["mi_cli"]), ("lark", config["lark_cli"]), ("codex", config["codex_cli"]))}
    if commands["mi"]:
        try:
            commands["mi"] = subprocess.run([config["mi_cli"], "--help"], capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            commands["mi"] = False
    secrets = {name: bool(secret_get(name)) for name in ("xunji_train_key", "xunji_diet_key", "xunji_food_search_key", "xunji_body_key", "xunji_template_key")}
    try:
        from mi_fitness_mcp.auth import load_mi_fitness_token

        user_id, pass_token = load_mi_fitness_token()
        mi_authenticated = bool(user_id and pass_token)
    except Exception:
        mi_authenticated = False
    with db() as conn:
        history = {
            "meta": conn.execute("select count(*) from meta").fetchone()[0],
            "writes": conn.execute("select count(*) from writes").fetchone()[0],
        }
    print(json.dumps({"commands": commands, "mi_authenticated": mi_authenticated, "secrets": secrets, "chat_id": bool(config.get("chat_id")), "model": model_diagnostic(config), "state": str(STATE_PATH), "history": history}, ensure_ascii=False, indent=2))
    return 0 if all(commands.values()) and all(secrets.values()) and mi_authenticated else 1


def plan_preview() -> str:
    preview = {
        "mode": "rolling_reference",
        "order": list(SPLIT_ORDER),
        "templates": {split: strength_template_plan(split) for split in stable_training_templates()},
        "fixed_activities": initial_week_plan(),
        "rule": "仅完成独立力量训练后推进；固定活动和漏练不推进；模板修改需确认",
    }
    text = json.dumps(preview, ensure_ascii=False, indent=2)
    print(text)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def self_check(data_dir: Path) -> int:
    """Run the one portable runtime check shipped with the coach."""
    set_data_dir(data_dir)
    with db() as conn:
        assert conn.execute("select 1").fetchone() == (1,)
    assert STATE_PATH == data_dir.resolve() / "private.db"
    with patch.dict(os.environ, {"BODY_FITNESS_DATA_DIR": ""}):
        assert _default_data_dir() == DEFAULT_DATA_DIR.resolve()
    assert "热身" not in today_recommendation(date(2026, 10, 10), "胸")
    print("self-check: private-db-path ok")
    original = DATA_DIR
    with tempfile.TemporaryDirectory(prefix="body coach migration ") as temp:
        root = Path(temp)
        legacy = root / "legacy"
        (legacy / "data").mkdir(parents=True)
        (legacy / "config.json").write_text(
            json.dumps({"chat_id": "chat-test", "body_profile": {"test_only": True}, "timezone": "Asia/Shanghai"}),
            encoding="utf-8",
        )
        with closing(sqlite3.connect(legacy / "data" / "state.db")) as conn:
            conn.execute("create table meta (key text primary key, value text not null)")
            conn.execute("create table writes (request_id text primary key, status text not null, response text)")
            conn.execute("insert into meta values ('keep', 'legacy')")
            conn.execute("insert into writes values ('one', 'ok', '{}')")
            conn.commit()
        set_data_dir(root / "destination")
        meta_set("keep", "newer")
        first = migrate_legacy(legacy)
        second = migrate_legacy(legacy)
        with db() as conn:
            assert conn.execute("select count(*) from writes").fetchone() == (1,)
        assert meta_get("keep") == "newer"
        assert load_config()["chat_id"] == "chat-test"
        assert first["writes"] == 1 and second["writes"] == 0
    set_data_dir(original)
    print("self-check: legacy-migration ok")
    fake_search = "0123456789abcdef" * 2
    fake_llm = "xjllm_" + "test-token"
    tokens = unique_tokens({"text": f"Bearer {fake_search} {fake_llm}"})
    assert tokens == [fake_llm, fake_search]
    previous = os.environ.get("LARK_CHANNEL_PROFILE")
    os.environ["LARK_CHANNEL_PROFILE"] = "keep-me"
    ensure_lark_channel_env("ignored")
    assert os.environ["LARK_CHANNEL_PROFILE"] == "keep-me"
    if previous is None:
        del os.environ["LARK_CHANNEL_PROFILE"]
    else:
        os.environ["LARK_CHANNEL_PROFILE"] = previous
    print("self-check: onboarding-and-bridge-env ok")
    assert model_diagnostic({"model_analysis": False, "codex_cli": "missing"})["reason"] == "disabled"
    assert model_diagnostic({"model_analysis": True, "codex_cli": "missing"})["reason"] == "missing_cli"
    original_model_error = meta_get("last_model_error")
    try:
        meta_set("last_model_error", json.dumps({"reason": "process_failed", "exit_code": 7, "detail": "safe"}))
        diagnostic = model_diagnostic({"model_analysis": True, "codex_cli": sys.executable})
        assert diagnostic["reason"] == "process_failed" and diagnostic["exit_code"] == 7
    finally:
        with db() as conn:
            if original_model_error is None:
                conn.execute("delete from meta where key='last_model_error'")
            else:
                conn.execute("insert or replace into meta(key,value) values('last_model_error',?)", (original_model_error,))
    print("self-check: model-diagnostic ok")
    fake_cache = {"activity": {}, "sleep": {}, "heart": {}, "body": {}}
    diet_with_prior_target = {
        "res": {
            "days": [
                {"date": "2026-10-09", "foods": {"limits": {"protein": 162, "carb": 216, "fat": 72}}},
                {
                    "date": "2026-10-10",
                    "totals": {"totalCal": 1437.315, "totalProtein": 71.424, "totalCarb": 154.866, "totalFat": 59.287},
                    "foods": {"limits": {"protein": 0, "carb": 0, "fat": 0}},
                },
            ]
        }
    }
    with patch(__name__ + ".mi_cache_overview", return_value=fake_cache):
        nutrition = local_summary({"status": "ok"}, {"training": [], "diet": diet_with_prior_target}, date(2026, 10, 11))
    assert "饮食热量：1437 / 2160 kcal" in nutrition
    assert "蛋白质：71 / 162 g" in nutrition
    print("self-check: nutrition-target-fallback ok")
    fake_xunji = {"training": [], "diet": {}, "body": {}}
    fake_config = {"model_analysis": False, "codex_cli": "missing", "chat_id": "chat-test", "lark_cli": "lark-cli"}
    with (
        patch(__name__ + ".load_config", return_value=fake_config),
        patch(__name__ + ".mi_snapshot", return_value={}),
        patch(__name__ + ".xunji_snapshot", return_value=fake_xunji),
        patch(__name__ + ".mi_cache_overview", return_value=fake_cache),
        patch(__name__ + ".mi_body_history", return_value=[]),
        patch(__name__ + ".post_json", return_value={"res": {"days": []}}),
        patch(__name__ + ".send_report", side_effect=AssertionError("dry-run sent a message")),
        patch(__name__ + ".sync_mi_workouts", side_effect=AssertionError("dry-run wrote a workout")),
        patch(__name__ + ".meta_set", side_effect=AssertionError("dry-run changed state")),
    ):
        assert run_daily(force=True, dry_run=True) == 0
        assert run_weekly(dry_run=True) == 0
        assert run_meal_reminder(dry_run=True) == 0
    print("self-check: report-dry-run ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Local personal fitness coach")
    parser.add_argument("--data-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    daily = sub.add_parser("daily")
    daily.add_argument("--force", action="store_true")
    daily.add_argument("--dry-run", action="store_true")
    weekly = sub.add_parser("weekly")
    weekly.add_argument("--dry-run", action="store_true")
    meal = sub.add_parser("meal-reminder")
    meal.add_argument("--dry-run", action="store_true")
    food = sub.add_parser("food")
    food.add_argument("--json-file", type=Path, required=True)
    food.add_argument("--source-id", required=True)
    secret = sub.add_parser("set-secret")
    secret.add_argument("name", choices=["xunji_train_key", "xunji_diet_key", "xunji_food_search_key", "xunji_body_key", "xunji_template_key"])
    sub.add_parser("plan-preview")
    sub.add_parser("self-check")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--legacy-root", type=Path, required=True)
    login = sub.add_parser("login-xiaomi")
    login.add_argument("--base-url", default="http://127.0.0.1:8321")
    login.add_argument("--timeout", type=int, default=300)
    keys = sub.add_parser("import-xunji-keys")
    keys.add_argument("--chat-id")
    keys.add_argument("--message-id", action="append", default=[])
    keys.add_argument("--start", required=True)
    keys.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    set_data_dir(args.data_dir or _default_data_dir())
    if args.command == "self-check":
        return self_check(DATA_DIR)
    if args.command == "migrate":
        print(json.dumps(migrate_legacy(args.legacy_root), sort_keys=True))
        return 0
    if args.command == "login-xiaomi":
        return login_xiaomi(args.base_url, args.timeout)
    if args.command == "import-xunji-keys":
        mappings = dict(item.split("=", 1) for item in args.message_id if "=" in item)
        return import_xunji_keys(args.chat_id or load_config().get("chat_id", ""), mappings, args.start, args.inspect)
    if args.command == "doctor":
        return doctor()
    if args.command == "daily":
        return run_daily(args.force, args.dry_run)
    if args.command == "weekly":
        return run_weekly(args.dry_run)
    if args.command == "meal-reminder":
        return run_meal_reminder(args.dry_run)
    if args.command == "food":
        return write_food(args.json_file, args.source_id)
    if args.command == "set-secret":
        secret_set(args.name, getpass.getpass(f"{args.name}: "))
        return 0
    if args.command == "plan-preview":
        print("confirmation_id=" + plan_preview())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
