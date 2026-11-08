"""global_memory / emotion_memory：会话标签与"什么不该进记忆库"。

这两个插件的核心风险是"把不该记的东西记下来"，所以重点测跳过规则。
"""

import pytest

from conftest import load_plugin


# ---------------- global_memory ----------------

@pytest.fixture(scope="module")
def gm():
    return load_plugin("astrbot_plugin_global_memory")


def test_label_group(gm):
    assert gm._label("snowluma:GroupMessage:873367976") == "群873367976"


def test_label_friend(gm):
    assert gm._label("snowluma:FriendMessage:10001") == "私聊"


def test_label_unknown_falls_back(gm):
    raw = "weird"
    assert gm._label(raw) == raw


# ---------------- emotion_memory ----------------

@pytest.fixture(scope="module")
def em():
    return load_plugin("astrbot_plugin_emotion_memory")


@pytest.fixture()
def inst(em):
    obj = object.__new__(em.EmotionMemory)
    obj.cfg = {"min_len": 8, "skip_prefixes": ["/", "！", "。"], "skip_questions": True}
    return obj


def test_skip_short_text(inst):
    assert inst._should_skip("嗯") is True


def test_skip_slash_command(inst):
    assert inst._should_skip("/学习 状态 看看现在怎么样") is True


def test_skip_question(inst):
    assert inst._should_skip("你还记得我们最早聊的是什么吗") is True


def test_skip_pure_emoji(inst):
    assert inst._should_skip("😂😂😂😂😂😂😂😂") is True


def test_keep_normal_sentence(inst):
    assert inst._should_skip("今天用户把机器人搬到自己的电脑上了") is False


def test_skip_question_can_be_disabled(inst):
    inst.cfg["skip_questions"] = False
    assert inst._should_skip("这个功能到底怎么用呢") is False
