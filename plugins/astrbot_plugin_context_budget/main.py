"""上下文预算：用「滞回裁剪」把每轮请求的历史控制在一个可预测的大小。

为什么不用 AstrBot 自带的 max_turns？
  - 自带的压缩按「轮数」算，不看真实体积；一条长消息就能把上下文顶到几十万 token。
  - 而且它每轮都可能重写历史头部，导致 DeepSeek 的前缀缓存反复失效（命中率在 99% 和 3% 之间跳）。

这里的做法是「阈值 + 低水位」：
  - 平时不动上下文，前缀保持不变，缓存持续命中；
  - 只有总字数超过 high_chars 才裁一次，且一次裁到 low_chars，下一次裁剪要再过很久。
  - 只改「发给模型的那一份」，数据库里的完整历史一个字都不动（recall_history 照样能翻）。

配置（plugin_data/astrbot_plugin_context_budget/config.json）：
  enabled        是否启用
  high_chars     触发裁剪的总字数（按字符粗略折半算 token）
  low_chars      裁剪后的目标字数
  keep_last      至少保留最近多少条消息（防止裁空）
  strip_images   是否把「较老消息」里的图片剥掉（只留最近 2 条里的图）
  image_keep     保留图片的消息条数
  log            每次裁剪写一行日志
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN = "astrbot_plugin_context_budget"
DATA = Path(__file__).resolve().parents[2] / "plugin_data" / PLUGIN
CONF = DATA / "config.json"

DEFAULTS = {
    "enabled": True,
    "high_chars": 60000,
    "low_chars": 40000,
    "keep_last": 8,
    "strip_images": True,
    "image_keep": 2,
    "log": True,
}


def _load_cfg(extra: dict | None = None) -> dict:
    """优先级：代码默认值 < plugin_data/config.json < WebUI（_conf_schema）配置。"""
    cfg = dict(DEFAULTS)
    try:
        if CONF.exists():
            cfg.update(json.loads(CONF.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[context_budget] 配置读取失败，用默认值：{exc}")
    if isinstance(extra, dict):
        # 空字符串（WebUI 未填的文本框）不覆盖已有配置
        cfg.update({
            k: v for k, v in extra.items()
            if k in DEFAULTS and v is not None
            and not (isinstance(v, str) and not v.strip())
        })
    return cfg


def _part_text(part) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        if part.get("type") == "text":
            return str(part.get("text") or "")
        if "text" in part and isinstance(part.get("text"), str):
            return part["text"]
        # 图片等非文本按固定体积估算，避免漏算
        if part.get("type") in ("image_url", "image"):
            return "\x00IMG\x00" * 200
    return ""


def msg_text(m) -> str:
    """把一条消息压成用于计量的文本。"""
    if isinstance(m, str):
        return m
    if not isinstance(m, dict):
        return str(m)
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(_part_text(p) for p in c)
    return ""


def has_image(m) -> bool:
    c = m.get("content") if isinstance(m, dict) else None
    if not isinstance(c, list):
        return False
    return any(
        isinstance(p, dict) and p.get("type") in ("image_url", "image")
        for p in c
    )


def strip_image(m, placeholder: str = "（这里原本有一张图，已经过时，不再重发）"):
    """把一条消息里的图片换成占位文本，返回新对象（不改原对象）。"""
    if not isinstance(m, dict) or not isinstance(m.get("content"), list):
        return m
    new_parts = []
    changed = False
    for p in m["content"]:
        if isinstance(p, dict) and p.get("type") in ("image_url", "image"):
            new_parts.append({"type": "text", "text": placeholder})
            changed = True
        else:
            new_parts.append(p)
    if not changed:
        return m
    out = dict(m)
    out["content"] = new_parts
    return out


def plan_trim(contexts: list, cfg: dict):
    """返回 (新列表, 裁掉条数, 裁剪前字数, 裁剪后字数)。纯函数，方便测试。"""
    high = int(cfg["high_chars"])
    low = int(cfg["low_chars"])
    keep_last = int(cfg["keep_last"])

    rows = list(contexts or [])
    before = sum(len(msg_text(m)) for m in rows)
    dropped = 0

    # 1) 先剥掉老消息里的图片（不改变条数，只减体积）
    if cfg.get("strip_images", True):
        keep_img = max(0, int(cfg.get("image_keep", 2)))
        for i in range(0, max(0, len(rows) - keep_img)):
            rows[i] = strip_image(rows[i])

    mid = sum(len(msg_text(m)) for m in rows)

    # 2) 没超过高水位就什么都不做（保住缓存前缀）
    if mid <= high:
        return rows, dropped, before, mid

    # 3) 超过才裁，一次裁到低水位
    total = mid
    while total > low and (len(rows) - dropped) > keep_last:
        total -= len(msg_text(rows[dropped]))
        dropped += 1
    return rows[dropped:], dropped, before, sum(len(msg_text(m)) for m in rows[dropped:])


@register(PLUGIN, "Whale Mind contributors", "上下文预算：滞回裁剪历史，保住前缀缓存", "1.0.0", "")
class ContextBudget(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.ctx = context
        self.webui_cfg = dict(config or {})
        self.n_trim = 0
        self.last = None

    async def initialize(self):
        cfg = _load_cfg(self.webui_cfg)
        DATA.mkdir(parents=True, exist_ok=True)
        if not CONF.exists():
            CONF.write_text(
                json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        logger.info(
            f"[context_budget] 已加载｜高水位 {cfg['high_chars']} 字"
            f"｜低水位 {cfg['low_chars']} 字｜剥老图={cfg.get('strip_images')}"
        )

    @filter.on_llm_request()
    async def budget(self, event: AstrMessageEvent, req):
        try:
            cfg = _load_cfg(self.webui_cfg)
            if not cfg.get("enabled", True):
                return
            contexts = getattr(req, "contexts", None)
            if not contexts:
                return
            rows, dropped, before, after = plan_trim(contexts, cfg)
            if dropped:
                req.contexts = rows
                self.n_trim += 1
                self.last = {"before": before, "after": after, "dropped": dropped}
                if cfg.get("log", True):
                    logger.info(
                        f"[context_budget] 上下文 {before:,} 字 > {cfg['high_chars']:,}"
                        f"｜裁掉 {dropped} 条 → {after:,} 字（第 {self.n_trim} 次）"
                    )
            elif after != before:
                # 只剥了图，条数没变
                req.contexts = rows
                if cfg.get("log", True):
                    logger.info(
                        f"[context_budget] 剥老消息图片：{before:,} → {after:,} 字"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[context_budget] 处理失败（已跳过）：{type(exc).__name__}: {exc}")
