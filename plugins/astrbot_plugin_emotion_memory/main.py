"""助手的第二层记忆（影子模式）。

当前只做「记」，不做「想」：
  每条用户消息都喂给 affective-memory，让它按情感权重、遗忘曲线自己维护记忆库；
  但**不往上下文里注入任何东西**，她的说话方式、记忆内容完全不受影响。

观察一周后再决定是否开启注入（config.json 里 shadow=false 才会注入）。

安全约束（对应代码审查发现的坑）：
  - 永远不调用 apply_pollution / 不读 polluted 视图，只存原文。
  - 任何异常都吞掉并记警告，绝不让聊天链路崩。
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_emotion_memory"
DATA_DIR = str(Path(__file__).resolve().parents[2] / "plugin_data" / PLUGIN_NAME)
VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
DEFAULTS = {
    "shadow": True,
    "enabled": True,
    "max_len": 500,
    "skip_prefixes": ["/", "！", "。"],
    "skip_questions": True,
    "min_len": 8,
    "stats_log": True,
}


@register(PLUGIN_NAME, "Whale Mind contributors", "第二层记忆：情感权重 + 遗忘曲线（影子模式，默认只记不注入）", "0.1.0")
class EmotionMemory(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.cfg = dict(DEFAULTS)
        self.cfg.update(config or {})
        self.sys = None
        self.ready = False
        self.n_written = 0
        self.n_skipped = 0
        self._task = None

    # ---------- 初始化 ----------
    def _boot(self):
        if VENDOR not in sys.path:
            sys.path.insert(0, VENDOR)
        from emotion_memory import EmotionalMemorySystem  # noqa: PLC0415

        os.makedirs(DATA_DIR, exist_ok=True)
        self.sys = EmotionalMemorySystem(base_dir=DATA_DIR)
        self.ready = True

    async def initialize(self):
        try:
            await asyncio.to_thread(self._boot)
            st = self.sys.stats()
            logger.info(
                f"[emotion_memory] 影子模式已启动｜库中 {st.get('memory_count', 0)} 条"
                f"｜shadow={self.cfg['shadow']}"
            )
        except Exception as e:  # noqa: BLE001
            self.ready = False
            logger.warning(f"[emotion_memory] 初始化失败（不影响聊天）：{e}")
        self._task = asyncio.create_task(self._stats_loop())

    async def _stats_loop(self):
        while True:
            try:
                await asyncio.sleep(6 * 3600)
                if self.ready and self.cfg.get("stats_log", True):
                    st = await asyncio.to_thread(self.sys.stats)
                    size = 0
                    p = os.path.join(DATA_DIR, "memories.jsonl")
                    if os.path.exists(p):
                        size = os.path.getsize(p)
                    logger.info(
                        f"[emotion_memory] 统计｜库 {st.get('memory_count', 0)} 条"
                        f"｜本轮写入 {self.n_written}｜跳过 {self.n_skipped}"
                        f"｜{size // 1024} KB"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[emotion_memory] 统计异常：{e}")

    async def terminate(self):
        if self._task:
            self._task.cancel()

    # ---------- 记：每条用户消息 ----------
    def _should_skip(self, text: str) -> bool:
        t = (text or "").strip()
        if len(t) < int(self.cfg.get("min_len", 8)):
            return True
        for p in self.cfg.get("skip_prefixes", []):
            if t.startswith(p):
                return True
        # 提问不是记忆：默认不把「你还记得…吗」「为什么会…」这类问句存进来
        if self.cfg.get("skip_questions", True) and t.rstrip("。.!！… ").endswith(
            ("吗", "呢", "么", "?", "？")
        ):
            return True
        # 纯表情/纯符号
        return not any(ch.isalnum() for ch in t)

    async def _remember(self, text, umo):
        try:
            await asyncio.to_thread(
                self.sys.remember, text, "chat", None, None, 0.6
            )
            self.n_written += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[emotion_memory] 写入失败：{e}")

    @filter.on_llm_request()
    async def on_request(self, event: AstrMessageEvent, req):
        if not self.ready or not self.cfg.get("enabled", True):
            return
        try:
            text = (req.prompt or "")[: int(self.cfg.get("max_len", 500))]
            # 注入块在 prompt 尾部，这里只取用户原话的前半段
            if "\n\n【" in text:
                text = text.split("\n\n【", 1)[0]
            if self._should_skip(text):
                self.n_skipped += 1
                return
            await self._remember(text.strip(), str(event.unified_msg_origin))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[emotion_memory] on_request 异常：{e}")
