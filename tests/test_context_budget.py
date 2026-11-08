"""context_budget：滞回裁剪的边界行为。

这是整套记忆系统里最容易出错、也最贵的一段逻辑——多裁一条她会忘事，少裁一条
每轮都要多付钱，所以每个分支都单独钉住。
"""

import pytest

from conftest import load_plugin


@pytest.fixture(scope="module")
def cb():
    return load_plugin("astrbot_plugin_context_budget")


@pytest.fixture()
def cfg(cb):
    c = dict(cb.DEFAULTS)
    c.update(
        {
            "enabled": True,
            "high_chars": 1000,
            "low_chars": 400,
            "keep_last": 3,
            "strip_images": True,
            "image_keep": 2,
        }
    )
    return c


def mk(n, size=200):
    return [{"role": "user", "content": "啊" * size} for _ in range(n)]


def test_below_high_water_keeps_everything(cb, cfg):
    rows = mk(3)
    out, dropped, before, after = cb.plan_trim(rows, cfg)
    assert dropped == 0
    assert len(out) == 3
    assert before == after


def test_above_high_water_trims_down_to_low(cb, cfg):
    rows = mk(10)
    out, dropped, before, after = cb.plan_trim(rows, cfg)
    assert dropped > 0
    assert after <= 400 + 200
    assert len(out) >= cfg["keep_last"]


def test_trim_keeps_the_newest_message(cb, cfg):
    rows = mk(10)
    out, *_ = cb.plan_trim(rows, cfg)
    assert out[-1] is rows[-1]


def test_hysteresis_does_not_trim_again_after_one_pass(cb, cfg):
    rows, *_ = cb.plan_trim(mk(10), cfg)
    rows = rows + [{"role": "user", "content": "嗯" * 100} for _ in range(2)]
    _, dropped2, _, _ = cb.plan_trim(rows, cfg)
    assert dropped2 == 0


def test_old_images_are_stripped_recent_ones_kept(cb, cfg):
    img = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "text", "text": "看图"},
        ],
    }
    rows = [img, img, img, img]
    out, _, before, after = cb.plan_trim(rows, cfg)
    assert after < before
    assert cb.has_image(out[-1]) and cb.has_image(out[-2])
    assert not cb.has_image(out[0])


def test_keep_last_guard_prevents_empty_result(cb):
    cfg = dict(cb.DEFAULTS)
    cfg.update({"high_chars": 100, "low_chars": 10, "keep_last": 5, "strip_images": False})
    rows = [{"role": "user", "content": "啊" * 500} for _ in range(4)]
    out, *_ = cb.plan_trim(rows, cfg)
    assert len(out) == 4


def test_empty_contexts_is_safe(cb, cfg):
    out, dropped, before, after = cb.plan_trim([], cfg)
    assert out == []
    assert dropped == 0
    assert before == 0 and after == 0


def test_msg_text_handles_str_and_multimodal(cb):
    assert cb.msg_text({"content": "纯文本"}) == "纯文本"
    assert cb.msg_text({"content": [{"type": "text", "text": "分"}]}) == "分"
