"""Controlled three-group WeChat 4.1 assistant with local memory."""

from __future__ import annotations

import getpass
import json
import os
import pathlib
import sqlite3
import sys
import time
import urllib.error

from .bridge import at_users, body, open_db
from . import memory


ROOT = pathlib.Path(os.environ.get("WECHAT_AI_HOME", pathlib.Path.cwd())).resolve()
CONFIG_PATH = ROOT / "config" / "groups.json"


def load_configs() -> list[dict]:
    configs = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["groups"]
    if not isinstance(configs, list) or not configs:
        raise RuntimeError("at least one group is required")
    if len({c.get("group_id") for c in configs}) != len(configs):
        raise RuntimeError("group IDs must be unique")
    if len({c.get("group_name") for c in configs}) != len(configs):
        raise RuntimeError("UI group names must be unique")
    if len({c.get("bot_id") for c in configs}) != 1 or len({c.get("owner_id") for c in configs}) != 1:
        raise RuntimeError("account IDs differ across groups")
    for config in configs:
        required = {"group_id", "group_name", "member_count", "member_ids", "owner_id", "bot_id"}
        if not required.issubset(config):
            raise RuntimeError("incomplete group configuration")
        count = config["member_count"]
        members = config["member_ids"]
        if not isinstance(count, int) or count < 3 or len(members) != count or len(set(members)) != count:
            raise RuntimeError("invalid pinned membership")
        if config["owner_id"] not in members or config["bot_id"] not in members:
            raise RuntimeError("test accounts absent")
    return configs


def checked_group(db, config: dict, all_groups: list[dict] | None = None) -> dict:
    if db.wxid != config["bot_id"]:
        raise RuntimeError("logged-in account changed")
    groups = db.get_groups() if all_groups is None else all_groups
    matches = [g for g in groups if g.get("username") == config["group_id"]]
    if len(matches) != 1:
        raise RuntimeError("pinned group ID not found")
    group = matches[0]
    if group.get("name") != config["group_name"] or sum(
        g.get("name") == config["group_name"] for g in groups
    ) != 1:
        raise RuntimeError("group name changed or is not unique")
    members = {str(m.get("username") or "") for m in group.get("members", [])}
    if int(group.get("member_count") or 0) != config["member_count"] or members != set(config["member_ids"]):
        raise RuntimeError("pinned group membership changed")
    return group


def eligible_message(db, config: dict, group: dict, msg: dict) -> tuple[bool, str]:
    sender = str(msg.get("sender_username") or "")
    if sender == config["bot_id"] or sender not in config["member_ids"] or msg.get("type") != "文本":
        return False, "sender-or-type"
    question = body(msg).strip()
    if not question:
        return False, "empty-text"
    raw = db.get_message_row(group["username"], msg["local_id"], shard=msg.get("shard"))
    if config["bot_id"] not in at_users((raw or {}).get("source")):
        return False, "no-real-at"
    return True, question


def send_text_with_checked_paste(ui, chat_name: str, text: str) -> bool:
    """Send only after the pinned chat and a full draft readback both match."""
    for attempt in range(2):
        if not ui.ensure_window() or ui.current_chat() != chat_name:
            return False
        edit = ui._chat_input()
        if edit is None:
            return False
        ui._paste_into(edit, text, clear=True)
        time.sleep(0.3)
        landed, _, _ = ui._paste_landed(edit, text)
        if landed:
            if ui.current_chat() != chat_name:
                return False
            try:
                edit.SendKeys("{Enter}", waitTime=0.05)
            except Exception:
                return False
            return True
        if attempt == 0:
            print("MULTI_PASTE_RETRY: empty or mismatched draft; no message sent", flush=True)
            time.sleep(0.5)
    if ui.ensure_window() and ui.current_chat() == chat_name:
        edit = ui._chat_input()
        if edit is not None:
            try:
                value_pattern = edit.GetValuePattern()
                if value_pattern is not None and not value_pattern.IsReadOnly:
                    value_pattern.SetValue(text)
                    time.sleep(0.3)
                    landed, _, _ = ui._paste_landed(edit, text)
                    if landed and ui.current_chat() == chat_name:
                        edit.SendKeys("{Enter}", waitTime=0.05)
                        print("MULTI_VALUE_FALLBACK_SENT", flush=True)
                        return True
            except Exception as exc:
                print("MULTI_VALUE_FALLBACK_ERROR: " + type(exc).__name__, flush=True)
    print("MULTI_INPUT_FAILED: no message sent", flush=True)
    return False


def send_one(db, config: dict, text: str) -> bool:
    try:
        group = checked_group(db, config)
    except RuntimeError as exc:
        print("MULTI_STOP: " + str(exc), flush=True)
        return False
    from wechatauto.uia_driver import WeChatUIA

    ui = WeChatUIA()
    ui._set_screen_reader_flag = lambda _enabled: False
    ui.wake = lambda: None
    try:
        if not ui.ensure_window():
            print("MULTI_STOP: WeChat UI unavailable", flush=True)
            return False
        if ui.current_chat() != config["group_name"]:
            opened = ui.open_chat(config["group_name"])
            if not opened:
                print("MULTI_CHAT_RETRY: target group not opened on first attempt", flush=True)
                time.sleep(1)
                opened = ui.open_chat(config["group_name"])
            if not opened:
                print("MULTI_STOP: could not open target group UI", flush=True)
                return False
        if ui.current_chat() != config["group_name"]:
            print("MULTI_STOP: target UI title mismatch", flush=True)
            return False
        before = db.get_messages(group["username"], limit=5)
        before_seq = max((int(m.get("sort_seq") or 0) for m in before), default=0)
        if ui.current_chat() != config["group_name"]:
            print("MULTI_STOP: target UI changed", flush=True)
            return False
        if not send_text_with_checked_paste(ui, config["group_name"], text):
            print("MULTI_SEND_FAILED: no retry", flush=True)
            return False
    except Exception as exc:
        print("MULTI_STOP: UI error: " + type(exc).__name__, flush=True)
        return False
    print("MULTI_SENT_UI: one message attempted", flush=True)
    for _ in range(6):
        time.sleep(2)
        recent = db.get_messages(group["username"], limit=20)
        if any(int(m.get("sort_seq") or 0) > before_seq and body(m) == text for m in recent):
            print("MULTI_SENT_DB_OK: reply present in pinned group", flush=True)
            return True
    print("MULTI_SEND_UNVERIFIED: no retry", flush=True)
    return False


def main() -> int:
    mode = "ai" if "--ai" in sys.argv else "monitor"
    print("MULTI_STARTING: mode=" + mode, flush=True)
    try:
        configs = load_configs()
        db = open_db()
        all_groups = db.get_groups()
        groups = {c["group_id"]: checked_group(db, c, all_groups) for c in configs}
        memory_db = memory.connect()
    except Exception as exc:
        print("MULTI_STOP: " + type(exc).__name__ + ": " + str(exc), flush=True)
        return 2
    if mode == "ai" and not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        key = getpass.getpass("Paste DeepSeek API key here (hidden input): ").strip()
        if not key:
            print("MULTI_STOP: key missing", flush=True)
            return 3
        os.environ["DEEPSEEK_API_KEY"] = key
    since = {}
    for config in configs:
        group_id = config["group_id"]
        history = db.get_messages(group_id, limit=100)
        since[group_id] = max((int(m.get("sort_seq") or 0) for m in history), default=0)
    print("MEMORY_READY: three pinned groups; old messages ignored", flush=True)
    deadline = time.monotonic() + 300 if mode != "ai" else None
    sent_count = 0
    while deadline is None or time.monotonic() < deadline:
        for config in configs:
            group_id = config["group_id"]
            try:
                messages = db.get_new_messages(group_id, since_seq=since[group_id], limit=100)
            except Exception as exc:
                print("MULTI_READ_ERROR: " + type(exc).__name__, flush=True)
                continue
            for msg in messages:
                since[group_id] = max(since[group_id], int(msg.get("sort_seq") or 0))
                try:
                    matched, detail = eligible_message(db, config, groups[group_id], msg)
                except Exception as exc:
                    print("MULTI_READ_ERROR: " + type(exc).__name__, flush=True)
                    continue
                if not matched:
                    continue
                print("MULTI_MATCHED: genuine @ from pinned member; group=" + str(config["member_count"]), flush=True)
                if mode == "monitor":
                    continue
                sender = str(msg.get("sender_username") or "")
                member = next((m for m in groups[group_id]["members"] if m.get("username") == sender), {})
                author_name = str(member.get("remark") or member.get("nick_name") or "群员")[:30]
                questioner = {
                    "name": author_name,
                    "member_key": next(
                        (f"member_{index}" for index, item in enumerate(groups[group_id]["members"])
                         if item.get("username") == sender), "unknown"
                    ),
                }
                try:
                    command = memory.parse_command(detail)
                    if command is not None:
                        reply = memory.handle_command(memory_db, command, config, sender, author_name)
                        print("MEMORY_COMMAND: " + command[0], flush=True)
                    else:
                        preference = memory.get_preference(memory_db, group_id, sender)
                        facts = memory.relevant_facts(memory_db, group_id, detail)
                        print(f"MEMORY_RETRIEVED: {len(facts)}", flush=True)
                        recent = db.get_messages(group_id, limit=100)
                        context = memory.recent_chat_context(
                            recent, int(msg.get("sort_seq") or 0), groups[group_id]["members"],
                            config["bot_id"], body,
                        )
                        print(f"CHAT_CONTEXT: {len(context)} text messages from previous 10", flush=True)
                        context, facts = memory.select_relevant_evidence(detail, questioner, context, facts)
                        print(f"EVIDENCE_SELECTED: chat={len(context)} memory={len(facts)}", flush=True)
                        reply = memory.answer_with_memory(detail, preference, facts, context, questioner)
                except (RuntimeError, urllib.error.URLError, ValueError, KeyError, IndexError, sqlite3.Error) as exc:
                    print("MULTI_MODEL_ERROR: " + type(exc).__name__, flush=True)
                    continue
                if not send_one(db, config, reply):
                    return 4
                sent_count += 1
                print(f"MULTI_REPLY_COUNT: {sent_count}", flush=True)
        time.sleep(3)
    print("MULTI_FINISHED: time limit reached", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
