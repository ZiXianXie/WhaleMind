"""recall_history —— 让她能主动翻自己的聊天记录。

背景：AstrBot 每轮只发最近 60 轮上下文，更早的对话她看不见；
而且 60 轮压缩会把 live 库里的老消息整段丢掉。
所以这里做两件事：
  1) 每日归档：把 live 库里的对话落到 archive/YYYY-MM-DD.jsonl（只增不删）
  2) 检索：live 库 + 归档文件一起搜，给她 recall_history 工具

设计要点：
- 对 live DB 只读，绝不写。
- 归档按 (ts, role, md5(text)) 去重，重复导入不会膨胀。
- 只收 role=user/assistant 的正文；think 段和 system_reminder 一律丢弃。
- 时间戳从 user 消息里的 <system_reminder>Current datetime: ...</system_reminder> 提取，
  assistant 消息沿用上一条 user 的时间。
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register

# <AstrBot>/data —— 插件在 data/plugins/<name>/main.py，向上两级就是 data
_DATA_ROOT = Path(__file__).resolve().parents[2]

# 全部可用环境变量覆盖，默认值只依赖安装位置，不含任何个人目录
DB_PATH = os.environ.get("WHALE_DB", str(_DATA_ROOT / "data_v4.db"))
PLUGIN_DATA = os.environ.get(
    "WHALE_PLUGIN_DATA",
    str(_DATA_ROOT / "plugin_data" / "astrbot_plugin_recall_history"),
)
ARCHIVE_DIR = os.path.join(PLUGIN_DATA, "archive")
# 可选：把每日归档推送到自己的电脑（留空 = 不推送，只存本地）
BRIDGE = os.environ.get("WHALE_BRIDGE", "")
BRIDGE_TOKEN = os.environ.get("WHALE_BRIDGE_TOKEN", "")
PC_DIR = os.environ.get("WHALE_HISTORY_DIR", "")
PLUGIN_NAME = "astrbot_plugin_recall_history"
AUTHOR = "Whale Mind contributors"
MAX_CHARS = 1400
SNIPPET_PAD = 60
ARCHIVE_HOUR = 4          # 每天 04:10 归档
ARCHIVE_MINUTE = 10

_REMIND_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)
_DT_RE = re.compile(r"Current datetime:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")

_CACHE = {}
_CACHE_AT = {}


def _apply_cfg(cfg: dict | None) -> None:
    """把 WebUI（_conf_schema）里的配置覆盖到模块级常量上。

    只接受已知键，且空值不覆盖——这样 WebUI 留空的项仍然走环境变量/默认值。
    """
    if not isinstance(cfg, dict):
        return
    g = globals()
    text_keys = {
        "bridge_url": "BRIDGE",
        "bridge_token": "BRIDGE_TOKEN",
        "history_dir": "PC_DIR",
    }
    int_keys = {
        "archive_hour": ("ARCHIVE_HOUR", 0, 23),
        "archive_minute": ("ARCHIVE_MINUTE", 0, 59),
        "max_chars": ("MAX_CHARS", 300, 8000),
    }
    for k, name in text_keys.items():
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            g[name] = v.strip()
    for k, (name, lo, hi) in int_keys.items():
        v = cfg.get(k)
        if isinstance(v, (int, float)):
            g[name] = max(lo, min(hi, int(v)))


def _text_of(content):
    parts = []
    for p in content or []:
        if isinstance(p, dict) and p.get("type") == "text":
            t = p.get("text") or ""
            if "<system_reminder>" in t:
                t = _REMIND_RE.sub("", t)
            if t.strip():
                parts.append(t.strip())
    return " ".join(parts).strip()


def _rec_key(ts, role, text):
    h = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()[:10]
    return f"{ts}|{role}|{h}"


def _iter_db_records(db_path, only_umo=None):
    """从一个 sqlite 库里抽记录。只读。"""
    out = []
    if not os.path.exists(db_path):
        return out
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return out
    try:
        if only_umo:
            rows = con.execute(
                "select user_id, content, created_at, updated_at from conversations where user_id=?",
                (only_umo,),
            ).fetchall()
        else:
            rows = con.execute(
                "select user_id, content, created_at, updated_at from conversations"
            ).fetchall()
    except Exception:
        rows = []
    finally:
        con.close()

    for uid, content, created, updated in rows:
        if "webchat" in str(uid):
            continue
        try:
            hist = json.loads(content)
        except Exception:
            continue
        ts = str(created or updated or "")[:16]
        for m in hist:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            mm = _DT_RE.search(json.dumps(m, ensure_ascii=False))
            if role == "user" and mm:
                ts = mm.group(1)
            text = _text_of(m.get("content"))
            if len(text) < 2:
                continue
            out.append((ts, role, text, str(uid)))
    return out


def _archive_files():
    if not os.path.isdir(ARCHIVE_DIR):
        return []
    return sorted(
        os.path.join(ARCHIVE_DIR, f)
        for f in os.listdir(ARCHIVE_DIR)
        if f.endswith(".jsonl")
    )


def _load_archive_records():
    seen = {}
    for path in _archive_files():
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    k = _rec_key(d.get("ts", ""), d.get("role", ""), d.get("text", ""))
                    if k not in seen:
                        seen[k] = (d.get("ts", ""), d.get("role", ""), d.get("text", ""), d.get("umo", ""))
        except Exception:
            continue
    return list(seen.values())


def _load_records(umo):
    """live 库 + 归档，去重后按时间排序。带 60 秒缓存。"""
    now = time.time()
    if umo in _CACHE and now - _CACHE_AT.get(umo, 0) < 60:
        return _CACHE[umo]

    src = None if umo == "__all__" else umo
    merged = {}
    for ts, role, text, uid in _load_archive_records() + _iter_db_records(DB_PATH, src):
        if umo != "__all__" and uid != umo:
            continue
        merged[_rec_key(ts, role, text)] = (ts, role, text, uid)

    records = sorted(merged.values(), key=lambda r: r[0])
    _CACHE[umo] = records
    _CACHE_AT[umo] = now
    return records


def _tokens(query):
    q = (query or "").strip()
    if not q:
        return []
    parts = [p for p in re.split(r"[\s,，、;；]+", q) if p]
    if len(parts) <= 1:
        return [q]
    return parts


def _snippet(text, tokens):
    pos = -1
    for t in tokens:
        pos = text.find(t)
        if pos >= 0:
            break
    if pos < 0:
        return text[: SNIPPET_PAD * 2]
    start = max(0, pos - SNIPPET_PAD)
    end = min(len(text), pos + SNIPPET_PAD)
    return ("…" if start else "") + text[start:end].replace("\n", " ") + ("…" if end < len(text) else "")


def archive_now(extra_dbs=None):
    """把 live 库（可选加上备份库）落进 archive。返回 (新增, 总计)。"""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    existing = {}
    for path in _archive_files():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                existing[_rec_key(d.get("ts", ""), d.get("role", ""), d.get("text", ""))] = True

    sources = [DB_PATH] + list(extra_dbs or [])
    buckets = {}
    added = 0
    for db in sources:
        for ts, role, text, uid in _iter_db_records(db):
            k = _rec_key(ts, role, text)
            if k in existing:
                continue
            existing[k] = True
            day = (ts[:10] if re.match(r"\d{4}-\d{2}-\d{2}", ts or "") else "unknown")
            buckets.setdefault(day, []).append(
                {"ts": ts, "role": role, "umo": uid, "text": text, "h": k.split("|")[-1]}
            )
            added += 1

    for day, rows in buckets.items():
        path = os.path.join(ARCHIVE_DIR, f"{day}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return added, sum(1 for _ in existing)


def push_archive_to_pc(day=None):
    """把归档文件推送到本机桥（未配置 WHALE_BRIDGE / WHALE_HISTORY_DIR 时直接跳过）。"""
    if not BRIDGE or not PC_DIR:
        return 0
    day = day or time.strftime("%Y-%m-%d")
    path = os.path.join(ARCHIVE_DIR, f"{day}.jsonl")
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        body = f.read()
    payload = json.dumps(
        {"path": os.path.join(PC_DIR, f"{day}.jsonl"), "content": body},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{BRIDGE}/fs/write",
        data=payload,
        headers={"Content-Type": "application/json", "X-Diary-Token": BRIDGE_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8"))
        return int(d.get("bytes") or 0) if d.get("verified", True) else 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[recall_history] 推送到电脑失败：{e}")
        return 0


@register(PLUGIN_NAME, AUTHOR, "翻聊天记录：检索全量历史对话", "1.1.0")
class RecallHistory(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.webui_cfg = dict(config or {})
        _apply_cfg(self.webui_cfg)
        self._task = None

    async def initialize(self):
        # 启动时补一次归档（把 live 库里还在的对话先存下来）
        try:
            added, total = await asyncio.to_thread(archive_now)
            logger.info(f"[recall_history] 启动归档：新增 {added} 条，归档共 {total} 条")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[recall_history] 启动归档失败：{e}")
        self._task = asyncio.create_task(self._daily_loop())

    async def _daily_loop(self):
        while True:
            try:
                now = time.localtime()
                target = time.mktime(
                    (now.tm_year, now.tm_mon, now.tm_mday, ARCHIVE_HOUR, ARCHIVE_MINUTE, 0, 0, 0, -1)
                )
                delay = target - time.time()
                if delay <= 0:
                    delay += 86400
                await asyncio.sleep(delay)
                added, total = await asyncio.to_thread(archive_now)
                day = time.strftime("%Y-%m-%d")
                size = await asyncio.to_thread(push_archive_to_pc, day)
                logger.info(
                    f"[recall_history] 每日归档 {day}：新增 {added} 条，归档共 {total} 条"
                    f"，推送电脑 {size} 字节"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[recall_history] 每日归档异常：{e}")
                await asyncio.sleep(600)

    async def terminate(self):
        if self._task:
            self._task.cancel()

    @llm_tool(name="recall_history")
    async def recall_history(
        self,
        event: AstrMessageEvent,
        query: str,
        scope: str = "here",
        limit: int = 5,
    ) -> str:
        """翻聊天记录：回忆很久以前说过的话、做过的事。当你想不起某件事的细节、或对方问"你还记得吗"时使用。

        注意：这里查的是历史存档，不是最近 60 轮的上下文。

        Args:
            query(string): 检索关键词，中文直接写不要加标点，多个词用空格分开。例如「搬家 方案」「火锅」
            scope(string): 检索范围，here=只查当前会话，all=查所有会话。默认 here
            limit(number): 最多返回几条，默认 5，最大 12
        """
        try:
            umo = "__all__" if str(scope).strip().lower() == "all" else str(event.unified_msg_origin)
            records = await asyncio.to_thread(_load_records, umo)
            tokens = _tokens(query)
            if not tokens:
                return "没给关键词，我不知道翻什么。"

            scored = []
            for ts, role, text, uid in records:
                score = 0
                for t in tokens:
                    if t and t in text:
                        score += text.count(t)
                if score:
                    scored.append((score, ts, role, text, uid))

            if not scored:
                return f"翻了 {len(records)} 条记录，没有一条提到「{query}」。可能比较早，或者当时没聊过这个。"

            scored.sort(key=lambda x: (-x[0], x[1]))
            scored = scored[: max(1, min(int(limit), 12))]

            lines = [f"翻到 {len(scored)} 条（共扫 {len(records)} 条记录）："]
            for i, (score, ts, role, text, uid) in enumerate(scored, 1):
                who = "用户" if role == "user" else "我"
                tag = "" if uid.endswith(str(event.get_sender_id())) else " [别处]"
                lines.append(f"{i}. {ts} {who}：{_snippet(text, tokens)}{tag}")

            return "\n".join(lines)[:MAX_CHARS]
        except Exception as e:  # noqa: BLE001
            return f"翻记录的时候出错了：{e}"
