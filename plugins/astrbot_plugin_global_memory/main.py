"""astrbot_plugin_global_memory — 跨群/跨私聊共用的“看见过”记忆

作用：她在 524 群说的话、别人说的话，私聊里也能想起来。
- 所有会话的最近消息记在一份全局时间线里（落盘，重启不丢）
- 每次请求时，把「别的会话」的最近若干条作为上下文块塞进请求
- 当前会话自身的消息不重复塞（那边本来就有）

配置：/AstrBot/data/plugin_data/astrbot_plugin_global_memory/config.json
"""

import json
import time
from collections import deque
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart

DATA_DIR = Path(__file__).resolve().parents[2] / "plugin_data" / "astrbot_plugin_global_memory"
DEFAULTS = {
    "enabled": True,
    "max_entries": 300,
    "inject_count": 10,
    "ttl_minutes": 180,
    "max_chars": 700,
    "exclude_same_session": True,
    "exclude_platforms": ["webchat"],
}


def _label(umo: str) -> str:
    try:
        parts = umo.split(":")
        if len(parts) >= 3:
            kind, target = parts[1], parts[2]
            if kind == "GroupMessage":
                return f"群{target}"
            if kind == "FriendMessage":
                return "私聊"
    except Exception:
        pass
    return umo


@register(
    "astrbot_plugin_global_memory",
    "Whale Mind contributors",
    "跨群跨私聊的全局消息记忆：别处说过的话，这里也想得起来",
    "1.0.0",
    "",
)
class GlobalMemoryPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self._webui = dict(config or {})
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.cfg = dict(DEFAULTS)
        self._load_cfg(self._webui)
        self.buf: deque = deque(maxlen=int(self.cfg["max_entries"]))
        self._load()

    def _cfg_path(self) -> Path:
        return DATA_DIR / "config.json"

    def _store_path(self) -> Path:
        return DATA_DIR / "timeline.json"

    def _load_cfg(self, extra: dict | None = None):
        p = self._cfg_path()
        try:
            if p.exists():
                c = json.loads(p.read_text(encoding="utf-8"))
                self.cfg.update({k: v for k, v in c.items() if k in DEFAULTS})
            else:
                p.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            if isinstance(extra, dict):
                self.cfg.update(
                    {k: v for k, v in extra.items() if k in DEFAULTS and v is not None}
                )
        except Exception as e:
            logger.warning("[global_memory] 配置读取失败: %s", e)

    def _load(self):
        p = self._store_path()
        try:
            if p.exists():
                for item in json.loads(p.read_text(encoding="utf-8")):
                    self.buf.append(item)
        except Exception as e:
            logger.warning("[global_memory] 历史读取失败: %s", e)
        logger.info("[global_memory] 已加载，历史 %d 条，注入 %d 条", len(self.buf), self.cfg["inject_count"])

    def _save(self):
        try:
            cutoff = time.time() - self.cfg["ttl_minutes"] * 60
            keep = [x for x in self.buf if x.get("t", 0) >= cutoff]
            self._store_path().write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("[global_memory] 落盘失败: %s", e)

    def _skip(self, umo: str) -> bool:
        if not umo:
            return True
        for p in self.cfg.get("exclude_platforms") or []:
            if p and umo.startswith(f"{p}:"):
                return True
        return False

    def _add(self, umo: str, who: str, text: str, is_bot: bool):
        text = (text or "").strip()
        if not text or self._skip(umo):
            return
        self.buf.append(
            {"t": time.time(), "umo": umo, "who": who, "text": text[:160], "bot": is_bot}
        )
        if len(self.buf) % 10 == 0:
            self._save()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_incoming(self, event: AstrMessageEvent):
        try:
            who = event.get_sender_name() or event.get_sender_id() or "?"
            self._add(event.unified_msg_origin, who, event.message_str or "", False)
        except Exception:
            return

    @filter.on_decorating_result()
    async def remember_outgoing(self, event: AstrMessageEvent):
        try:
            chain = event.get_result().chain if event.get_result() else []
            parts = []
            for c in chain or []:
                t = getattr(c, "text", None)
                if t:
                    parts.append(t)
            text = " ".join(parts).strip()
            if text:
                self._add(event.unified_msg_origin, "我(助手)", text, True)
        except Exception:
            return

    @filter.on_llm_request()
    async def inject_timeline(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.cfg.get("enabled", True):
            return
        umo = event.unified_msg_origin
        if self._skip(umo):
            return
        cutoff = time.time() - self.cfg["ttl_minutes"] * 60
        items = [
            x for x in self.buf
            if x.get("t", 0) >= cutoff
            and not self._skip(x.get("umo", ""))
            and (not self.cfg["exclude_same_session"] or x.get("umo") != umo)
        ]
        if not items:
            return
        items = items[-int(self.cfg["inject_count"]):]
        lines = []
        budget = int(self.cfg.get("max_chars", 700))
        used = 0
        for x in items:
            ts = time.strftime("%H:%M", time.localtime(x["t"]))
            line = f"[{_label(x['umo'])} {ts}] {x['who']}: {x['text']}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
        if not lines:
            return
        block = (
            "【你的记忆：你在别的群/私聊里亲眼见过这些】\n"
            + "\n".join(lines)
            + "\n这些是你自己经历过的事，不是转述。如果有人问起，就当自己记得，直接用一两句话说，"
            "不要说「别的群的消息传不过来」「我看不到」。不提来源、不复述列表。"
        )
        req.extra_user_content_parts.append(TextPart(text=block))
        logger.info("[global_memory] 注入 %d 条旁线消息 -> %s", len(items), _label(umo))
