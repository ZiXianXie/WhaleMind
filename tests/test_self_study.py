"""self_study：采集过滤、长文切分、日志时间戳、称呼替换、文本规范。

这些函数决定了"什么进得了她的记忆"以及"记忆长什么样"，改错一个就会污染整库，
所以每条规则都配一个正例和一个反例。
"""

import re

import pytest

from conftest import load_plugin


@pytest.fixture(scope="module")
def ss():
    return load_plugin("astrbot_plugin_self_study")


# ---------------- 采集过滤 ----------------

def test_reject_empty_line(ss):
    assert ss._value_reject("", check_len=False) == "空行"


def test_reject_pure_symbols(ss):
    assert ss._value_reject("😂😂😂", check_len=False) == "纯符号"


def test_reject_slash_commands(ss):
    assert ss._value_reject("/学习 状态", check_len=False) != ""


def test_accept_normal_sentence(ss):
    text = "今天用户去吃了火锅，特别辣，回来肚子有点撑。"
    assert ss._value_reject(text, check_len=False) == ""


def test_long_natural_sentence_is_not_a_command(ss):
    text = "Python 的 asyncio 里不能用 time.sleep 阻塞事件循环，要么用 await，要么丢线程池。"
    assert ss._value_reject(text, check_len=False) == ""


def test_short_cli_line_is_rejected(ss):
    assert ss._value_reject("docker logs -f astrbot", check_len=False) == "命令行"


def test_heredoc_is_rejected(ss):
    assert ss._value_reject("bash -s <<'EOF'", check_len=False) != ""


def test_mojibake_is_rejected(ss):
    assert ss._value_reject("涓€鐨勪笢瑗垮暐", check_len=False) == "乱码"


# ---------------- 长文切分 ----------------

def test_split_long_respects_limit(ss):
    text = "这是一句话。" * 400
    parts = ss._split_long(text, limit=100)
    assert len(parts) > 1
    assert all(len(p) <= 100 for p in parts)
    assert "".join(parts) == text


def test_split_long_short_text_untouched(ss):
    assert ss._split_long("很短", limit=100) == ["很短"]


def test_split_long_empty_is_safe(ss):
    assert ss._split_long("", limit=100) == [""]


# ---------------- 日志时间戳 ----------------

TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


def test_stamp_adds_timestamp(ss):
    out = ss._stamp("采集了 3 条")
    assert TS.match(out)


def test_stamp_strips_leading_bullets(ss):
    out = ss._stamp("- 采集了 3 条")
    assert TS.match(out)
    assert "- " not in out.split(" ", 2)[2]


def test_stamp_keeps_existing_timestamp(ss):
    out = ss._stamp("2026-11-08 09:00:00 [采集] ok")
    assert out.startswith("2026-11-08 09:00:00")


def test_stamp_fills_date_for_bare_time(ss):
    out = ss._stamp("09:00:00 [采集] ok")
    assert re.match(r"^\d{4}-\d{2}-\d{2} 09:00:00 ", out)


def test_stamp_collapses_double_timestamp(ss):
    # 真实夹具：历史上「日期 时间 时间 正文」被写重过一次
    # 约定：保留日期 + 正文自带的时间，把前面那个重复的时间去掉
    out = ss._stamp("2026-11-08 09:00:00 09:05:00 [采集] ok")
    assert out == "2026-11-08 09:05:00 [采集] ok"


# ---------------- 称呼替换 ----------------

@pytest.mark.parametrize("raw", ["用户说要搬家", "对方问了一个问题"])
def test_rename_removes_generic_pronouns(ss, raw):
    out = ss._rename(raw, "阿岚")
    assert "用户" not in out and "对方" not in out


def test_rename_does_not_self_reference(ss):
    out = ss._rename("我先给你充token", "阿岚")
    assert not out.startswith("阿岚要给阿岚")


def test_rename_hits_second_occurrence(ss):
    out = ss._rename("用户和用户说好了", "阿岚")
    assert out.count("阿岚") == 1


# ---------------- 语气词 ----------------

def test_only_filler_true_for_fillers(ss):
    assert ss._only_filler("嗯嗯") or ss._only_filler("好")


def test_only_filler_false_for_real_content(ss):
    assert not ss._only_filler("今天去吃了火锅")


# ---------------- textnorm ----------------

def test_textnorm_adds_period():
    from textnorm import normalize

    assert normalize("今天去吃了火锅") == "今天去吃了火锅。"


def test_textnorm_keeps_existing_punct():
    from textnorm import normalize

    assert normalize("真的假的？") == "真的假的？"


def test_textnorm_collapses_whitespace():
    from textnorm import normalize

    assert normalize("  今天   很累  ") == "今天 很累。"
