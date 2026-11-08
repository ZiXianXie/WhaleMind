
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASTRBOT_HOME = Path(os.environ.get("ASTRBOT_HOME", str(REPO / ".demo" / "astrbot")))
"""在演示实例已经建好表之后，灌入虚构的对话历史。"""

import json
import sqlite3
import time
import uuid
from pathlib import Path

DB = ASTRBOT_HOME / "data" / "data_v4.db"

UIDS = ["demo:FriendMessage:10001", "demo:FriendMessage:10002", "demo:GroupMessage:20001"]

DIALOG = [
    ("明天七点叫我起床，别让我睡过头", "行，七点整。要是你赖床，我就连叫三次。"),
    ("今天下雨了，记得带伞", "知道啦。你出门也带一把，别淋湿。"),
    ("帮我看下这个报错，KeyError: 'user_id'", "字典里没这个键。先确认上游有没有返回 user_id。"),
    ("我想给记忆系统加一个「忘记」的能力", "让它自己决定忘什么有点危险。可以先做只读的权重衰减。"),
    ("你觉不觉得我最近太拼了", "有一点。你昨晚两点还在改那个插件。"),
    ("晚饭吃了没", "吃了，番茄鸡蛋面。你呢，别又只喝咖啡。"),
    ("这个函数为什么这么慢", "每次都在全量重写 JSONL。改成追加写，只在合并时重写。"),
    ("要是让你自己挑一个功能加深，你选哪个", "记忆。别的功能都是锦上添花。"),
    ("我今天心情不太好", "那就不聊正事了。要不要说说发生了什么？"),
    ("周末想去爬山", "记得看天气。山顶风大，带件外套。"),
]

con = sqlite3.connect(DB)
now = time.time()
for idx, uid in enumerate(UIDS):
    hist = []
    base = now - 86400 * 2
    for i, (user, bot) in enumerate(DIALOG):
        ts = base + i * 3600
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        hist.append({
            "role": "user",
            "content": [{"type": "text", "text": f"{user}<system_reminder>Current datetime: {stamp} (CST)</system_reminder>"}],
            "timestamp": ts,
        })
        hist.append({
            "role": "assistant",
            "content": [{"type": "text", "text": bot}],
            "timestamp": ts + 8,
        })
    cid = uuid.uuid4().hex
    con.execute(
        """INSERT OR REPLACE INTO conversations
           (created_at, updated_at, inner_conversation_id, conversation_id,
            platform_id, user_id, content, title, persona_id, token_usage)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 86400 * 3)),
         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60 * idx)),
         idx + 1, cid, "demo", uid,
         json.dumps(hist, ensure_ascii=False), "演示会话", "dafeiyu", 0),
    )
con.commit()

rows = con.execute("select user_id, length(content) from conversations").fetchall()
con.close()
print(f"已写入 {len(rows)} 个会话：")
for uid, size in rows:
    print(f"  {uid}  {size} 字符")
