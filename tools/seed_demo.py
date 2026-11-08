
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASTRBOT_HOME = Path(os.environ.get("ASTRBOT_HOME", str(REPO / ".demo" / "astrbot")))
"""为演示实例造一份完全虚构的数据（不碰真实实例的任何文件）。

角色设定：主人「阿岚」和她的助手「小满」。
用途：README 截图、插件市场展示图。
"""

import json
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = ASTRBOT_HOME
DATA = ROOT / "data"
PD = DATA / "plugin_data"

FAKE_UIDS = [
    "demo:FriendMessage:10001",
    "demo:FriendMessage:10002",
    "demo:GroupMessage:20001",
]

DIALOG = [
    ("alarm", "明天七点叫我起床，别让我睡过头", "行，七点整。要是你赖床，我就连叫三次。"),
    ("life", "今天下雨了，记得带伞", "知道啦。你出门也带一把，别淋湿。"),
    ("work", "帮我看下这个报错，KeyError: 'user_id'", "字典里没这个键。先确认上游有没有返回 user_id。"),
    ("idea", "我想给记忆系统加一个「忘记」的能力", "让它自己决定忘什么有点危险。可以先做只读的权重衰减。"),
    ("chat", "你觉不觉得我最近太拼了", "有一点。你昨晚两点还在改那个插件。"),
    ("life", "晚饭吃了没", "吃了，番茄鸡蛋面。你呢，别又只喝咖啡。"),
    ("work", "这个函数为什么这么慢", "每次都在全量重写 JSONL。改成追加写，只在合并时重写。"),
    ("idea", "要是让你自己挑一个功能加深，你选哪个", "记忆。别的功能都是锦上添花。"),
    ("chat", "我今天心情不太好", "那就不聊正事了。要不要说说发生了什么？"),
    ("life", "周末想去爬山", "记得看天气。山顶风大，带件外套。"),
]

PEOPLE = [
    ("阿岚", "主人。做游戏开发的，最近在折腾记忆系统。"),
    ("小满", "助手。说话短，爱较真，不喜欢被叫主人。"),
    ("老周", "阿岚的同事，负责后端。"),
]


def ensure(d: Path):
    d.mkdir(parents=True, exist_ok=True)


def seed_conversations():
    db = DATA / "data_v4.db"
    ensure(DATA)
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE IF NOT EXISTS conversations (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT UNIQUE,
               platform_id TEXT,
               content TEXT,
               created_at TEXT,
               updated_at TEXT,
               title TEXT,
               persona_id TEXT,
               token_usage INTEGER DEFAULT 0)"""
    )
    now = time.time()
    for idx, uid in enumerate(FAKE_UIDS):
        hist = []
        base = now - 86400 * 2
        for i, (kind, user, bot) in enumerate(DIALOG):
            ts = base + i * 3600
            t = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            hist.append({
                "role": "user",
                "content": [{"type": "text", "text": f"{user}<system_reminder>Current datetime: {t} (CST)</system_reminder>"}],
                "timestamp": ts,
            })
            hist.append({
                "role": "assistant",
                "content": [{"type": "text", "text": bot}],
                "timestamp": ts + 8,
            })
        con.execute(
            "INSERT OR REPLACE INTO conversations (user_id, platform_id, content, created_at, updated_at, title, persona_id, token_usage) VALUES (?,?,?,?,?,?,?,?)",
            (uid, "demo", json.dumps(hist, ensure_ascii=False), now - 86400 * 3, now - 60 * idx,
             "演示会话", "dafeiyu", 0),
        )
    con.commit()
    con.close()
    print(f"会话已写入 {db}（{len(FAKE_UIDS)} 个）")


def seed_vault():
    """memory_vault 的对话流水。"""
    d = PD / "astrbot_plugin_memory_vault"
    ensure(d)
    rows = []
    now = time.time()
    for i in range(240):
        kind, user, bot = DIALOG[i % len(DIALOG)]
        who = random.choice(["阿岚", "阿岚", "阿岚", "老周"])
        rows.append({
            "ts": int((now - (240 - i) * 1800) * 1000),
            "umo": random.choice(FAKE_UIDS),
            "who": who,
            "bot": bot,
            "text": user,
        })
    with (d / "index.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"流水已写入 {d / 'index.jsonl'}（{len(rows)} 条）")


def seed_self_study():
    """facts / style / emotion，给星图和注入用。"""
    d = PD / "astrbot_plugin_self_study"
    ensure(d / "digest")
    facts = [
        ("阿岚是游戏开发者，最近在做一个长期记忆系统。", 0.92),
        ("阿岚不喜欢被称呼为「主人」，更习惯直接叫名字。", 0.88),
        ("阿岚习惯在深夜改代码，经常两点之后才睡。", 0.81),
        ("阿岚对成本敏感，会盯着每次调用的 token 账单。", 0.79),
        ("阿岚养了一只叫「豆子」的橘猫。", 0.74),
        ("阿岚在做一个叫「Whale Mind」的开源项目。", 0.71),
        ("老周是阿岚的同事，负责后端服务。", 0.66),
        ("阿岚讨厌写文档，但总会逼自己补上。", 0.63),
    ]
    styles = [
        ("说话短，一般一到两句，不用长段落。", 0.9),
        ("爱用「行」开头表示同意。", 0.72),
        ("被人质疑时会先承认，再补一句理由。", 0.68),
        ("偶尔用「算了」结束一个话题。", 0.55),
    ]
    with (d / "digest" / "facts.jsonl").open("w", encoding="utf-8") as f:
        for text, conf in facts:
            f.write(json.dumps({
                "id": f"f{abs(hash(text)) % 10**10}", "time": "2026-11-08 09:00:00",
                "type": "fact", "text": text, "confidence": conf, "weight": conf,
                "hits": random.randint(1, 9), "source": "digest",
                "source_raw": "index.jsonl:12#a1b2c3d4",
            }, ensure_ascii=False) + "\n")
    with (d / "digest" / "style.jsonl").open("w", encoding="utf-8") as f:
        for text, conf in styles:
            f.write(json.dumps({
                "id": f"s{abs(hash(text)) % 10**10}", "time": "2026-11-08 09:00:00",
                "type": "style", "text": text, "confidence": conf, "weight": conf,
                "hits": random.randint(1, 5), "source": "digest", "aggregate": True,
                "source_raw": "index.jsonl:3-9",
            }, ensure_ascii=False) + "\n")
    print(f"事实 {len(facts)} 条、风格 {len(styles)} 条已写入 {d}")


def seed_galaxy():
    stars = []
    names = ["阿岚", "小满", "老周", "豆子", "Whale Mind", "记忆系统"]
    for i, n in enumerate(names * 4):
        stars.append({"id": f"n{i}", "name": n if i < len(names) else f"{n}{i}",
                      "group": n, "weight": round(random.uniform(0.2, 1.0), 2)})
    msgs = []
    now = time.time()
    for i in range(600):
        kind, user, bot = DIALOG[i % len(DIALOG)]
        msgs.append({"key": random.choice(FAKE_UIDS), "at": int((now - (600 - i) * 900) * 1000),
                     "self": bot, "text": user})
    out = REPO / "docs" / "images" / "galaxy-demo.json"
    ensure(out.parent)
    out.write_text(json.dumps({"stars": stars, "messages": msgs}, ensure_ascii=False), encoding="utf-8")
    print(f"星图演示数据已写入 {out}（{len(stars)} 星 / {len(msgs)} 消息）")


if __name__ == "__main__":
    random.seed(20261108)
    seed_conversations()
    seed_vault()
    seed_self_study()
    seed_galaxy()
    print("演示数据生成完毕")
