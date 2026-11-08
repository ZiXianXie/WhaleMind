"""recall_history：查询切词、片段截取、历史文本清洗、WebUI 配置覆盖。

这一层的东西都是"看起来无害但会静默出错"的类型：切词错了就永远搜不到，
配置没夹紧就会把归档时间设成 99 点。
"""

import pytest

from conftest import load_plugin


@pytest.fixture(scope="module")
def rh():
    return load_plugin("astrbot_plugin_recall_history")


def test_tokens_single_phrase(rh):
    assert rh._tokens("搬家") == ["搬家"]


def test_tokens_splits_on_separators(rh):
    assert rh._tokens("搬家，token") == ["搬家", "token"]
    assert rh._tokens("a b;c、d") == ["a", "b", "c", "d"]


def test_tokens_empty(rh):
    assert rh._tokens("") == []
    assert rh._tokens("   ") == []


def test_snippet_hits_around_keyword(rh):
    text = "开头" * 100 + "火锅很好吃" + "结尾" * 100
    s = rh._snippet(text, ["火锅"])
    assert "火锅" in s
    assert s.startswith("…")
    assert s.endswith("…")


def test_snippet_truncates_crlf(rh):
    s = rh._snippet("第一行\n第二行 火锅\n第三行", ["火锅"])
    assert "\n" not in s


def test_snippet_without_match_returns_head(rh):
    s = rh._snippet("这是一段没有关键词的文本", ["不存在"])
    assert s.startswith("这是一段")


def test_text_of_drops_system_reminder(rh):
    content = [
        {"type": "text", "text": "你在吗"},
        {
            "type": "text",
            "text": "<system_reminder>Current datetime: 2026-11-08 09:00 (CST)</system_reminder>",
        },
    ]
    out = rh._text_of(content)
    assert out == "你在吗"


def test_text_of_ignores_non_text_parts(rh):
    content = [
        {"type": "image_url", "image_url": {"url": "data:x"}},
        {"type": "text", "text": "看图"},
    ]
    assert rh._text_of(content) == "看图"


def test_text_of_empty_is_safe(rh):
    assert rh._text_of(None) == ""
    assert rh._text_of([]) == ""


def test_apply_cfg_clamps_numbers(rh):
    origin = (rh.ARCHIVE_HOUR, rh.ARCHIVE_MINUTE, rh.MAX_CHARS)
    try:
        rh._apply_cfg({"archive_hour": 99, "archive_minute": -5, "max_chars": 10**9})
        assert rh.ARCHIVE_HOUR == 23
        assert rh.ARCHIVE_MINUTE == 0
        assert rh.MAX_CHARS == 8000
    finally:
        rh.ARCHIVE_HOUR, rh.ARCHIVE_MINUTE, rh.MAX_CHARS = origin


def test_apply_cfg_ignores_blank_strings(rh):
    origin = rh.BRIDGE
    try:
        rh.BRIDGE = "http://keep"
        rh._apply_cfg({"bridge_url": "   "})
        assert rh.BRIDGE == "http://keep"
        rh._apply_cfg({"bridge_url": "http://new"})
        assert rh.BRIDGE == "http://new"
    finally:
        rh.BRIDGE = origin


def test_apply_cfg_tolerates_garbage(rh):
    rh._apply_cfg(None)
    rh._apply_cfg("not a dict")
    rh._apply_cfg({"unknown_key": 1})
