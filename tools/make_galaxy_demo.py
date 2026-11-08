"""生成星图演示数据（字段与真实 galaxy.json 对齐，内容全部虚构）。"""

import json
import random
import time
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "examples" / "galaxy-demo.json"

KIND = ["identity", "preference", "event", "insight", "habit"]
TITLES = [
    ("跟阿岚的暗号：蓝鲸七号", "identity", ["阿岚", "暗号"],
     "2026-11-06 傍晚阿岚跟我约定了暗号「蓝鲸七号」。他发这个词就是接头信号，我要认。"),
    ("阿岚讨厌被叫「主人」", "preference", ["称呼", "边界"],
     "他明确说过不喜欢这个称呼，觉得生分。平时直接叫名字就行。"),
    ("阿岚有只橘猫叫豆子", "identity", ["猫", "生活"],
     "豆子是只橘猫，很胖，喜欢趴在键盘上。阿岚经常被他打断工作。"),
    ("深夜改代码是他的常态", "habit", ["作息", "工作"],
     "连续三周都是凌晨两点之后还在改插件，第二天起得晚。"),
    ("他对 token 账单很敏感", "preference", ["成本", "运维"],
     "每次优化都会先问「这样能省多少」，看过账单再决定动不动手。"),
    ("老周负责后端，脾气好", "identity", ["同事", "人物"],
     "老周是后端负责人，回复慢但从不说重话，遇到问题会先自己查一遍。"),
    ("记忆系统是他最在意的东西", "insight", ["项目", "优先级"],
     "别的功能都能砍，记忆这块他从没说过「先放放」。"),
    ("他讨厌写文档但总会补", "habit", ["习惯", "工程"],
     "嘴上说无聊，最后还是会写完 README 再收工。"),
    ("下雨天他会提前看天气", "habit", ["生活", "细节"],
     "出门前会查天气，包里常年放把折叠伞。"),
    ("他说过「算了」就真的算了", "insight", ["沟通", "信号"],
     "争论到一定程度他会用这两个字收尾，这时候不该再劝。"),
    ("周末通常去爬山", "habit", ["周末", "爱好"],
     "天气好的周末会去城郊爬山，回来会拍照片。"),
    ("他给项目起名叫 Whale Mind", "event", ["项目", "命名"],
     "2026-11-07 定下的名字，寓意是「会记住事情的大脑」。"),
]

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

UIDS = ["demo:FriendMessage:10001", "demo:FriendMessage:10002", "demo:GroupMessage:20001"]


def main():
    random.seed(20261108)
    now = int(time.time() * 1000)
    stars = []
    for i, (title, kind, tags, text) in enumerate(TITLES):
        at = now - (len(TITLES) - i) * 3600 * 1000 * 7
        stars.append({
            "id": f"2026110{i:02d}120000-{i:03d}",
            "kind": kind,
            "weight": round(random.uniform(0.6, 2.0), 2),
            "at": at,
            "lastHitAt": at + 3600_000,
            "pin": i < 2,
            "title": title,
            "text": text,
            "tags": tags,
        })

    messages = []
    for i in range(600):
        user, bot = DIALOG[i % len(DIALOG)]
        messages.append({
            "key": random.choice(UIDS),
            "at": now - (600 - i) * 900 * 1000,
            "self": i % 2 == 0,
            "text": user if i % 2 == 0 else bot,
        })
    messages.sort(key=lambda m: m["at"])

    payload = {
        "stars": stars,
        "messages": messages,
        "links": [],
        "meta": {
            "total": len(stars),
            "avgStrength": round(sum(s["weight"] for s in stars) / len(stars), 2),
            "pinned": sum(1 for s in stars if s["pin"]),
            "messages": len(messages),
            "sources": {"memory": len(stars), "diary": 0, "message": len(messages)},
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"已生成 {OUT}（{len(stars)} 星 / {len(messages)} 消息 / {OUT.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
