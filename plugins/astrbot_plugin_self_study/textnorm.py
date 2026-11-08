"""公共文本规范：句末标点 + 空白压缩。digest 与所有落盘路径共用这一份。"""
import re

END_PUNCT = ("。", "！", "？", "…", "～", "~", ".", "!", "?")
_WS = re.compile(r"\s+")


def ensure_period(text: str) -> str:
    """条目结尾补句末标点：没有 。！？… 就补一个「。」。"""
    s = str(text or "").strip()
    if not s:
        return s
    if s.endswith(END_PUNCT):
        return s
    return s + "。"


def normalize(text: str) -> str:
    """压空白 + 补句末标点，落库前统一走这里。"""
    s = _WS.sub(" ", str(text or "")).strip()
    return ensure_period(s)
