"""助手的自主学习：采集 → 消化 → 人格 → 注入 四层。

数据落在配置里的 pc_root（留空则只存本机 plugin_data 下）
    raw/       每轮对话、网页正文、手动喂进来的视频转写（jsonl，一行一条）
    digest/    facts.jsonl（事实类）、style.jsonl（风格类）
    persona/   current.md、candidate.diff、versions/<时间戳>.md
    开关.json  可单独关掉人格层

设计要点：
- 本地（容器内 plugin_data）是写入缓冲与真值，PC 是持久落盘；桥挂了就本地攒着，恢复后补推。
- 向量相似度用字符 bigram 词袋 + 余弦，纯标准库，0.85 以上判同一条 → 更新而不是新增。
- 人格补丁只做增/删/改行，单次最多 3 行；合并前过三道自检，旧版带时间戳存 versions。
- 注入层走 on_llm_request，自动拼「你记得这些」到系统提示后面，不依赖工具调用。
"""

import asyncio
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

sys.path.insert(0, str(Path(__file__).parent))
try:  # 公共标点模块：和 live digest 共用同一份规则
    from textnorm import normalize, ensure_period  # noqa: E402
except Exception:  # pragma: no cover - 极端情况下退回内联实现
    def normalize(text: str) -> str:
        s = str(text or "").strip()
        return s if not s or s.endswith(("。", "！", "？", "…", ".", "!", "?")) else s + "。"

    ensure_period = normalize  # type: ignore

PLUGIN = "astrbot_plugin_self_study"
LOCAL = Path(__file__).resolve().parents[2] / "plugin_data" / PLUGIN
CFG = LOCAL / "config.json"

DEFAULTS = {
    "bridge_url": "http://127.0.0.1:8899",
    "token": os.environ.get("WHALE_BRIDGE_TOKEN", ""),
    "pc_root": os.environ.get("WHALE_STUDY_DIR", ""),
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen3:8b-16k",
    "digest_use_cloud": True,
    "cloud_model": "deepseek-chat",
    "admins": [],
    "digest_hour": 3,
    "sim_threshold": 0.85,
    "inject_facts": 5,
    "inject_style": 3,
    "max_persona_ops": 3,
    "persona_max_chars": 4200,
    "persona_id": "",
    "enabled": True,
}

DIM = 512

# 人格自检：每进程只跑一次，手动重跑才算 force；两次之间至少隔这么久（秒）
SELFCHECK_MIN_GAP = 3600
_selfcheck_done = False
_selfcheck_at = 0.0
_selfcheck_cache = ""

# 人格按「# 一、」「# 二、」这种标题分节；一到三节是核心区
SECTION_RE = re.compile(r"^#\s*([一二三四五六])、")

# 语义冲突校验：否定类核心行当对照集，阈值 0.6
NEG_WORDS = ("不许", "绝不", "禁止", "别", "永远不要", "不能", "不")
FINAL_PARTICLES = "呗嘛啦呀哦哈"
MD_CHARS = set("#*`>[]|_")
CONF_SIM = 0.6

# 日志里「补丁校验 通过｜…｜版本 YYYYMMDD_HHMMSS」的解析式
LOG_PASS_RE = re.compile(r"\[补丁校验\] 通过｜(?P<ops>.*)｜版本 (?P<ver>\d{8}_\d{6}(?:_\d+)?)")
MAX_LOG_BYTES = 2 * 1024 * 1024   # 单个日志超过 2MB 就切
LOG_KEEP = 5                      # 最多留 5 份 .1~.5


# ---------------- 基础工具 ----------------
def _cfg_load(extra: dict | None = None) -> dict:
    try:
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cfg = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if v is not None})
    return out


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


# 消化时间窗：单次最多 900 秒；预估超过 800 秒就劈成两天
DIGEST_BUDGET_SECONDS = 900
DIGEST_SPLIT_ESTIMATE = 800
DIGEST_DEFAULT_SPEED = 137.0  # 字/秒，来自实测 29375 字 / 213.7 秒

_TS_LEAD = re.compile(
    r"^\s*[\[\(（【]?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?|\d{2}:\d{2}:\d{2})[\]\)）】]?\s*"
)
_JUNK_LEAD = re.compile(r"^[\s\-–—*·•>#=+~_|、,，。]+")
_DOUBLE_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}\s+(\d{2}:\d{2}:\d{2})\s+(.*)$", re.S
)


def _stamp(line: str) -> str:
    """统一日志行首为时间戳：剥掉「- 」「* 」这类前导符号，缺时间戳就补，重复的不留。"""
    s = _JUNK_LEAD.sub("", str(line or "").strip()).strip()
    if not s:
        return f"{_now()}".rstrip()
    m = _DOUBLE_TS.match(s)
    if m:  # 「日期 时间 时间 正文」→ 只留第一个时间和正文里的原时间
        return f"{m.group(1)} {m.group(2)} {m.group(3)}".rstrip()
    m = _TS_LEAD.match(s)
    if m:
        stamp = m.group(1)
        if re.fullmatch(r"\d{2}:\d{2}:\d{2}", stamp):  # 只有时分秒就补日期
            stamp = f"{time.strftime('%Y-%m-%d')} {stamp}"
        return f"{stamp} {s[m.end():]}".strip()
    return f"{_now()} {s}".rstrip()


def _embed(text: str) -> list:
    """字符 bigram（中文）+ 小写词（英文/数字）词袋，哈希到固定维度后归一化。"""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    vec = [0.0] * DIM
    if not t:
        return vec
    grams = []
    for i in range(len(t) - 1):
        g = t[i:i + 2]
        if g.strip():
            grams.append(g)
    # 中文单字也给半权重：短句里「吃/饭/今」这类字往往才是真正对得上的信号
    for ch in t:
        if "\u4e00" <= ch <= "\u9fff":
            grams.append(ch)
    for w in re.findall(r"[a-z0-9_]{2,}", t):
        grams.append(w)
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        vec[h % DIM] += 0.5 if len(g) == 1 else 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm:
        vec = [v / norm for v in vec]
    return vec


def _cos(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))


def _charset(text: str) -> set:
    t = re.sub(r"\s+", "", str(text or "")).lower()
    chars = {c for c in t if "\u4e00" <= c <= "\u9fff"}
    words = set(re.findall(r"[a-z0-9_]{2,}", t))
    return chars | words


def _contain(a: set, b: set) -> float:
    if not a:
        return 0.0
    return len(a & b) / float(len(a))


# 这些是给机器人自己下命令的行，不属于「对话内容」，不许进 raw
CMD_WORDS = ("学习", "收录", "消化", "试改", "改人格", "回滚", "注入开关", "开关人格", "接话", "voice", "mnemon", "provider", "model")
QUOTE_RE = re.compile(r"[「『\"“]([^」』\"”]{1,24})[」』\"”]")


def _looks_like_command(text: str) -> bool:
    t = str(text or "").strip().lstrip("/／").strip()
    if not t:
        return False
    if t.startswith("学习"):
        return True
    head = t[:14]
    return any(w in head for w in CMD_WORDS)


# ---- 价值过滤：进 raw 之前先过一遍，命中一条理由就丢（记日志 + 计数）----
VALUE_WORDS = (
    "学习", "收录", "消化", "试改", "改人格", "回滚", "注入开关", "开关人格",
    "接话", "提示词", "自检", "一致性", "打补丁", "现在呢", "压测",
)
SHELL_HEADS = (
    "docker", "python", "python3", "echo", "cat", "cd ", "ls ", "curl", "git", "npm",
    "pip", "sudo", "systemctl", "ssh", "scp", "powershell",
)
HEREDOC_RE = re.compile(r"<<-?\s*['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?")
# GBK 被当成 UTF-8 解出来的典型字，「涓€」这种一串里至少会撞上两个
MOJIBAKE_CHARS = set("涓鐨鍜浜锛銆鐢鍥欏鏄濡浣犳兂鎴戦噷杩囧彲涔堝啀閿欒潃")


def _count_cjk(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


SHORT_CJK = 6          # 单条中文短句阈值（只用于合并判断，不再用来丢弃）
SHORT_LAT = 12
BURST_WINDOW = 180     # 同一个人三分钟内的短句攒成一组
BURST_MAX = 5
LONG_SPLIT = 2000      # 超过就按句切条入库
HUGE_LIMIT = 50000     # 超过就拒收，只留摘要
_SENT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_FILLER_CHARS = set("嗯哦噢额呃啊呀哈嘿诶唉喂好了行吧么呢吗嘛咦欸唔嘛嗷")


def _only_filler(text: str) -> bool:
    """整句只有语气词/应答词，没有实义内容。"""
    t = re.sub(r"[\s\W_]+", "", text or "")
    return bool(t) and all(c in _FILLER_CHARS for c in t)


def _split_long(text: str, limit: int = LONG_SPLIT) -> list:
    """按句子边界切成长度 ≤ limit 的多条，避免把长文整条塞进 raw。"""
    out, buf = [], ""
    for part in _SENT_RE.findall(text or ""):
        if len(buf) + len(part) > limit and buf:
            out.append(buf)
            buf = ""
        while len(part) > limit:
            out.append(part[:limit])
            part = part[limit:]
        buf += part
    if buf:
        out.append(buf)
    return out or [str(text or "")[:limit]]


def _value_reject(text: str, check_len: bool = True) -> str:
    """这条文本值不值得进 raw：返回丢弃理由，空串表示可以收。

    check_len=False 时不按长度砍（对话短句走合并判断，见 _flush_burst）。
    """
    s = str(text or "").strip()
    if not s:
        return "空行"
    compact = re.sub(r"\s+", "", s)
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", compact):
        return "纯符号"
    if "\ufffd" in s or sum(1 for ch in s if ch in MOJIBAKE_CHARS) >= 2:
        return "乱码"
    cjk_all = _count_cjk(compact)
    # 一句自然语言里出现 "Python 的 asyncio…" 这种开头不该当命令行；
    # 只有短句或没什么中文的才套指令/命令行启发式。
    if cjk_all < 10:
        if _looks_like_command(s) or any(compact.startswith(w) for w in VALUE_WORDS):
            return "指令行"
        low = compact.lower()
        if any(low.startswith(h.strip()) for h in SHELL_HEADS) or HEREDOC_RE.search(s):
            return "命令行"
    if not check_len:
        return ""
    if cjk_all:
        if cjk_all < 6:
            return "中文过短"
    elif sum(1 for ch in compact if ch.lower().isalpha()) < 12:
        return "英文过短"
    return ""


DIGEST_SYS = (
    "你是记忆整理器。把对话/网页/转写里值得长期记住的东西压缩成短条目。"
    "输出 JSON：{\"facts\":[{\"text\":\"…\",\"from\":\"chat|web|video\",\"refs\":[行号]}],"
    "\"style\":[{\"text\":\"…\",\"refs\":[行号],\"aggregate\":true|false}]}。"
    "facts 记：谁是谁、发生过什么事、偏好与习惯，每条一句中文，≤40 字；"
    "用对话里出现过的实际称呼（对方的名字或昵称），不要写成「用户」「他」这种代称；"
    "对话双方是「对方」（人类）和「你」（助手）。条目里用对方的实际称呼指他，"
    "用「你」指助手自己，别把方向写反（「我先给你充token」= 对方要给助手充 token）。"
    "from 填这条信息的来源：直接对话写 chat，网页写 web，转写写 video。"
    "refs 必填：每条 fact 后面列出它依据的输入行号（行首方括号里的数字），可多行，"
    "比如 [3,4]；不确定就填最相关的那一行。"
    "style 记：她最近爱用的词、口头禅、语气走向，每条一句中文、≤30 字，必须带上具体词或句式，"
    "比如「爱用『行吧』收尾」「常把『我靠』放句首」，不许写「使用某字描述某事」这种元描述。"
    "style 的 refs 允许 0~3 个行号：能从少数几行归纳出来就填那几行（比如 [7] 或 [12,13]）；"
    "多行归纳、确实指不出具体行时，refs 填空数组 [] 并把 aggregate 置 true，别硬凑行号。"
    "铁律一：只写原文里已经发生的事。打算、计划、将来、假设、如果，一律写成「打算/计划…」，"
    "绝不许写成「已经…了」。"
    "铁律二：不许加原文没有的评价或判断，尤其「觉得」「认为」「想要」「应该」这类字样，"
    "原文没说就别写。"
    "只写确定的信息，不臆测；没有就返回空数组。"
)


DIGEST_VERSION = "digest-v3-20261103"
DUP_WINDOW_SECONDS = 600

VAGUE_WORDS = ("偶尔", "有时", "有时候", "倾向于", "表现出", "体现出", "那种", "某种", "类似", "比喻", "情绪", "风格", "方式")


def _is_concrete(line: str) -> bool:
    """人格新行必须落到具体的词或句式上：要么带引号的具体说法，要么是能直接照说的一句话。

    像「偶尔用食物比喻情绪」这种带模糊词的模板直接判虚。
    """
    s = str(line or "").strip()
    if not s:
        return False
    if QUOTE_RE.search(s):
        return True
    if any(w in s for w in VAGUE_WORDS):
        return False
    body = re.sub(r"^[-*]\s*", "", s).strip()
    if body in ("很好", "不错", "还行", "一般", "正常", "没问题"):
        return False
    return len(body) >= 4


def _speaker_of(rows: list) -> str:
    """从 raw 的 source 里猜出对话对方的称呼（source 形如 umo|某人(10001)）。"""
    names = []
    for r in rows:
        src = str(r.get("source") or "")
        m = re.search(r"\|([^|()]{1,12})\(", src)
        if m:
            names.append(m.group(1).strip())
    if not names:
        return "用户"
    return max(set(names), key=names.count)


PRONOUNS = ("我", "你", "您", "咱", "咱们", "你们", "我们")


def _rename(text: str, speaker: str) -> str:
    """条目里不许出现「用户/对方」，换成对话里的实际称呼。

    白名单：我/你/您/咱 这些本来就该保留的人称不动，避免出现「阿岚要给阿岚充token」这种自指。
    若句子里已经出现过称呼，替换时退回「你」，不重复堆名字。
    """
    s = str(text or "").strip()
    for bad in ("用户", "对方"):
        if bad in s:
            already = any(p in s for p in (speaker,))
            s = s.replace(bad, "你" if already else speaker)
    s = s.replace("他 ", speaker + " ")
    # 兜底：同一句里称呼出现两次，把后一次换成「你」
    if s.count(speaker) >= 2:
        head, _, tail = s.rpartition(speaker)
        if speaker in head:
            s = head + "你" + tail
    return s


def _read_jsonl(path: Path) -> list:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    except FileNotFoundError:
        pass
    return rows


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _norm_rows(rows: list, kind: str) -> list:
    """老条目补 time/type/source_raw，并按 id 去重（防止历史追加重复）。"""
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict) or not str(r.get("text") or "").strip():
            continue
        rid = str(r.get("id") or "")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        r.setdefault("time", r.get("created") or r.get("updated") or _now())
        r.setdefault("type", kind)
        r.setdefault("source_raw", "")
        out.append(r)
    return out


# ---------------- 桥客户端 ----------------
class Bridge:
    def __init__(self, cfg: dict):
        self.base = str(cfg["bridge_url"]).rstrip("/")
        self.token = str(cfg["token"])

    def call(self, method: str, route: str, params=None, payload=None, timeout=25):
        url = self.base + route
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Diary-Token", self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": f"HTTP {exc.code}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------- 主插件 ----------------
@register(PLUGIN, "Whale Mind contributors", "自主学习：采集/消化/人格/注入", "1.0.0", "")
class SelfStudyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = _cfg_load()
        LOCAL.mkdir(parents=True, exist_ok=True)
        for sub in ("raw", "digest", "persona", "persona/versions", "spool"):
            (LOCAL / sub).mkdir(parents=True, exist_ok=True)
        self.bridge = Bridge(self.cfg)
        self._queue: list = []
        self._facts: list = []
        self._style: list = []
        self._fact_vecs: list = []
        self._style_vecs: list = []
        self._state = self._load_state()
        self._dropped: dict = {}
        self._bursts: dict = {}
        self._pending_persona = None
        self._tasks: list = []
        self._last_digest_day = ""
        self._stop = False
        # 人格自检结果：core_lines.txt 里的行必须逐字出现在现人格里，否则本次不加载人格层
        self.persona_ok = True
        self.persona_missing: list = []

    # -------- 状态与落盘 --------
    def _load_state(self) -> dict:
        p = LOCAL / "state.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"consumed": {}, "last_digest": "", "persona_checks": 0}

    def _save_state(self) -> None:
        (LOCAL / "state.json").write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _pc_path(self, *parts: str) -> str:
        return str(Path(self.cfg["pc_root"]).joinpath(*parts))

    def _push(self, rel: str, content: str, mode: str = "overwrite") -> bool:
        """推一份到电脑；失败就排队，等下一轮补推。

        注意（2026-11-02）：电脑上现役的桥是旧构建，`POST /fs/write` 只回
        `{"ok": true, "path": ..., "bytes": N}`，**不返回 verified 字段**。
        所以这里的成功判定退一步用「返回的 bytes 是否等于内容的 UTF-8 字节数」兜底；
        等下一轮统一升级桥（带 verified + 回读比对）后，再改成优先看 verified。
        """
        br = getattr(self, "bridge", None)
        if br is None:
            return False
        res = br.call(
            "POST", "/fs/write",
            payload={"path": self._pc_path(*rel.split("/")), "content": content, "mode": mode},
        )
        sent = len(content.encode("utf-8"))
        if res.get("ok") and int(res.get("bytes") or -1) == sent:
            return True
        if res.get("ok"):
            logger.warning(f"[self_study] 推电脑字节数对不上：{rel} 写入 {res.get('bytes')} / 应为 {sent}")
        self._queue.append({"rel": rel, "mode": mode})
        logger.warning(f"[self_study] 推电脑失败（已排队）：{res.get('error') or '字节数不一致'}")
        return False

    def _pc_delete(self, rel: str) -> bool:
        """删电脑上的镜像文件（桥是旧构建：只认 path，不返回 verified）。"""
        res = self.bridge.call("POST", "/fs/delete",
                               payload={"path": self._pc_path(*rel.split("/"))})
        return bool(res.get("ok"))

    def _flush_queue(self) -> None:
        if not self._queue:
            return
        local = list(self._queue)
        self._queue = []
        for item in local:
            p = LOCAL / item["rel"]
            if not p.exists():
                continue
            if not self._push(item["rel"], p.read_text(encoding="utf-8"), item["mode"]):
                return

    def _switch(self) -> dict:
        """开关文件：PC 优先，读不到就用本地镜像。"""
        res = self.bridge.call("GET", "/fs/read", {"path": self._pc_path("开关.json"), "max": 4000})
        raw = None
        if res.get("ok"):
            raw = res.get("text")
        if not raw:
            try:
                raw = (LOCAL / "switch.json").read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                raw = None
        if not raw:
            return {"persona_layer": True}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                data = {}
        except Exception:  # noqa: BLE001
            data = {}
        data.setdefault("persona_layer", True)
        return data

    # -------- 采集层 --------
    def _raw_file(self, day: str) -> Path:
        return LOCAL / "raw" / f"{day}.jsonl"

    def _log(self, evt: str, detail: str = "") -> None:
        """一行一条，按天落盘，同时镜像到 pc_root/logs；超 2MB 切分。
        「自检」「维护」只在内容变化时才写，状态没变就不落行（省得一天翻下来全是重复）。"""
        try:
            if evt in ("自检", "维护"):
                last = (self._state.get("log_last") or {}).get(evt) or ""
                if last == detail:
                    return
                self._state.setdefault("log_last", {})[evt] = detail
                self._save_state()
            d = LOCAL / "logs"
            d.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d")
            p = d / f"{day}.log"
            line = _stamp(f"[{evt}] {detail}")
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._rotate_log(p)
            self._push(f"logs/{day}.log", p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 写日志失败：{type(exc).__name__}: {exc}")

    def _audit_digest(self, batch_md5, chunk, size, sources, model_used, data,
                      cache_hit: bool) -> None:
        """把「消化」这一批的输入与模型原始输出一起存档，供事后核查。

        落盘到 digest/audit/<日期>/<时间>_<md5>.json，单批输入正文最多留 4000 字。
        """
        try:
            d = LOCAL / "digest" / "audit" / time.strftime("%Y-%m-%d")
            d.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%H%M%S")
            rows = [str(x)[:200] for _k, x in chunk][:120]
            payload = {
                "ts": _now(),
                "batch_md5": batch_md5,
                "model": model_used,
                "cache_hit": bool(cache_hit),
                "in_rows": len(chunk),
                "in_chars": size,
                "in_text": "\n".join(rows)[:4000],
                "out_facts": len((data or {}).get("facts") or []),
                "out_style": len((data or {}).get("style") or []),
                "out_raw": json.dumps(data, ensure_ascii=False)[:4000],
                "source_sample": sources[:8],
            }
            p = d / f"{stamp}_{batch_md5[:10]}.json"
            p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 消化审计写入失败：{type(exc).__name__}: {exc}")

    def _normalize_logs(self) -> None:
        """把已有日志行统一成「时间戳开头」：去掉行首的「- 」「* 」等符号，缺时间戳的补上。"""
        try:
            d = LOCAL / "logs"
            if not d.exists():
                return
            fixed = 0
            for p in sorted(d.glob("*.log")):
                lines = p.read_text(encoding="utf-8").splitlines()
                out = []
                for line in lines:
                    new = _stamp(line)
                    if new != line.strip():
                        fixed += 1
                    out.append(new)
                if out:
                    self._atomic_write(p, "\n".join(out) + "\n")
            if fixed:
                logger.info(f"[self_study] 日志格式统一：修正 {fixed} 行")
                self._push(f"logs/{time.strftime('%Y-%m-%d')}.log",
                           (d / f"{time.strftime('%Y-%m-%d')}.log").read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 日志格式统一失败：{type(exc).__name__}: {exc}")

    def _rotate_log(self, p: Path) -> None:
        """单文件超过 MAX_LOG_BYTES 就滚成 .1，旧的依次后移，只留 LOG_KEEP 份。"""
        try:
            if not p.exists() or p.stat().st_size <= MAX_LOG_BYTES:
                return
            Path(str(p) + f".{LOG_KEEP}").unlink(missing_ok=True)
            for i in range(LOG_KEEP - 1, 0, -1):
                src = Path(str(p) + f".{i}")
                if src.exists():
                    src.replace(Path(str(p) + f".{i + 1}"))
            p.replace(Path(str(p) + ".1"))
            p.touch()
            logger.info(f"[self_study] 日志已切分：{p.name} → {p.name}.1（保留 {LOG_KEEP} 份）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 日志切分失败：{type(exc).__name__}: {exc}")

    def _write_core_check(self, rows: list, missing: list) -> None:
        """自检单独出一份 JSON：总数 / 找到几条 / 缺哪几条。"""
        try:
            d = LOCAL / "logs"
            d.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": _now(),
                "persona_id": self._persona_id(),
                "total": len(rows),
                "found": len(rows) - len(missing),
                "missing_count": len(missing),
                "missing": missing,
                "ok": not missing,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            p = d / "core_check.json"
            self._atomic_write(p, text)
            self._push("logs/core_check.json", text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 写 core_check.json 失败：{type(exc).__name__}: {exc}")

    def _record(self, typ: str, source: str, text: str) -> None:
        """采集总入口：对话短句先攒组，其余直接存；超长按句切或拒收。"""
        text = (text or "").strip()
        if not text:
            return
        if typ == "chat" and self._is_short(text):
            self._burst_add(source, text)
            return
        self._flush_burst(source)
        key = hashlib.md5(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()[:12]
        seen = self._state.setdefault("dup10", {})
        now_ts = time.time()
        if now_ts - float(seen.get(key) or 0) < DUP_WINDOW_SECONDS:
            cnt = self._state.setdefault("dup_count", {})
            cnt[key] = int(cnt.get(key) or 1) + 1
            self._log("折叠", f"10 分钟内重复「{text[:24]}」｜累计 {cnt[key]} 次，不再入库")
            if len(seen) > 500:
                cut = now_ts - DUP_WINDOW_SECONDS
                for k in [k for k, v in seen.items() if float(v) < cut]:
                    seen.pop(k, None)
            return
        seen[key] = now_ts
        self._store(typ, source, text)

    @staticmethod
    def _is_short(text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        cjk = _count_cjk(compact)
        if cjk:
            return cjk < SHORT_CJK
        return sum(1 for ch in compact if ch.isalpha()) < SHORT_LAT

    def _burst_add(self, source: str, text: str) -> None:
        """同一个人三分钟内的短句先攒着，合起来再判有没有信息。"""
        now = time.time()
        b = self._bursts.get(source)
        if b and now - b["last"] <= BURST_WINDOW and len(b["items"]) < BURST_MAX:
            b["items"].append(text)
            b["last"] = now
            return
        if b:
            self._flush_burst(source)
        self._bursts[source] = {"items": [text], "first": now, "last": now}

    def _flush_burst(self, source: str) -> None:
        b = self._bursts.pop(source, None)
        if not b or not b["items"]:
            return
        items = b["items"]
        if len(items) == 1:
            # 单条短句不丢：原样入库，交给消化层判断
            self._store("chat", source, items[0], check_len=False)
            return
        merged = " / ".join(items)
        if _only_filler(merged):
            self._dropped["短句无实质"] = self._dropped.get("短句无实质", 0) + 1
            self._log("采集过滤", f"丢弃（{len(items)} 条短句合并后只有语气词）：{merged[:40]}")
            return
        self._store("chat", source, f"[{len(items)} 条短句合并] {merged}", check_len=False)
        self._log("采集", f"短句合并 {len(items)} 条：{merged[:50]}")

    def _flush_stale_bursts(self) -> None:
        now = time.time()
        for src in [s for s, b in self._bursts.items() if now - b["last"] > BURST_WINDOW]:
            self._flush_burst(src)

    def _store(self, typ: str, source: str, text: str, check_len: bool = True) -> None:
        if len(text) > HUGE_LIMIT:
            head = re.sub(r"\s+", " ", text[:200])
            self._dropped["超长拒收"] = self._dropped.get("超长拒收", 0) + 1
            self._log("采集过滤", f"超长拒收 {len(text)} 字，只留摘要：{head[:60]}")
            self._store_one(typ, source, f"[超长内容已拒收：原文 {len(text)} 字，只留开头] {head}",
                            check_len=False)
            return
        if len(text) > LONG_SPLIT:
            parts = _split_long(text)
            self._log("采集", f"超长切分 {len(text)} 字 → {len(parts)} 条")
            for part in parts:
                self._store_one(typ, source, part, check_len)
            return
        self._store_one(typ, source, text, check_len)

    def _store_one(self, typ: str, source: str, text: str, check_len: bool = True) -> None:
        reason = _value_reject(text, check_len=check_len)
        if reason:
            self._dropped[reason] = self._dropped.get(reason, 0) + 1
            if reason == "指令行":
                logger.info(f"[self_study] 指令行不进 raw：{text[:24]}")
            self._log("采集过滤", f"丢弃（{reason}）{text[:60]}")
            return
        day = time.strftime("%Y-%m-%d")
        row = {"type": typ, "source": source, "time": _now(), "text": text[:4000]}
        path = self._raw_file(day)
        _append_jsonl(path, row)
        self._push(f"raw/{day}.jsonl", path.read_text(encoding="utf-8"))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True):
            return
        try:
            text = (event.message_str or "").strip()
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            who = event.get_sender_name() if hasattr(event, "get_sender_name") else ""
            sid = event.get_sender_id() if hasattr(event, "get_sender_id") else ""
            if text:
                self._record("chat", f"{umo}|{who}({sid})", text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 采集失败：{exc}")

    def _ingest_spool(self) -> int:
        """web_search 的 fetch_url 会把正文追加到 spool/web.jsonl。"""
        spool = LOCAL / "spool" / "web.jsonl"
        if not spool.exists():
            return 0
        rows = _read_jsonl(spool)
        if not rows:
            return 0
        for r in rows:
            self._record("web", str(r.get("url") or "fetch_url"), str(r.get("text") or ""))
        spool.write_text("", encoding="utf-8")
        return len(rows)

    # -------- 消化层 --------
    def _new_raw(self) -> list:
        consumed = self._state.setdefault("consumed", {})
        fresh = []
        for path in sorted((LOCAL / "raw").glob("*.jsonl")):
            rows = _read_jsonl(path)
            start = int(consumed.get(path.name, 0))
            if start < len(rows):
                fresh.extend(rows[start:])
                consumed[path.name] = len(rows)
        self._save_state()
        return fresh

    def _ollama(self, system: str, user: str, timeout: int = 120) -> str:
        url = str(self.cfg["ollama_url"]).rstrip("/") + "/api/chat"
        payload = {
            "model": self.cfg["ollama_model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            # 不再压 num_ctx：模型本身已烤进 16384，这里写 8192 反而把长料截断（会退回空 JSON）
            "options": {"temperature": 0.0, "seed": 42},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return (body.get("message") or {}).get("content") or ""

    def _cloud_llm(self, system: str, user: str, timeout: int = 120) -> str:
        """走云端（deepseek）跑消化；配置从 AstrBot 的 cmd_config.json 里读。"""
        cfg_path = Path(__file__).resolve().parents[2] / "cmd_config.json"
        if not cfg_path.is_file():
            raise RuntimeError("找不到 cmd_config.json")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        src = None
        for item in cfg.get("provider_sources") or []:
            if str(item.get("provider") or "") == "deepseek":
                src = item
                break
        if not src:
            raise RuntimeError("没有 deepseek 源")
        keys = src.get("key") or []
        if not keys:
            raise RuntimeError("deepseek 没有可用 key")
        base = str(src.get("api_base") or "https://api.deepseek.com").rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        payload = {
            "model": str(self.cfg.get("cloud_model") or "deepseek-chat"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(base + "/chat/completions", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {keys[0]}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    def _llm_digest(self, system: str, user: str) -> tuple:
        """优先云端，云端不可用再降级本地；返回 (输出文本, 实际模型名)。"""
        if bool(self.cfg.get("digest_use_cloud", True)):
            try:
                out = self._cloud_llm(system, user)
                if out.strip():
                    return out, "cloud:" + str(self.cfg.get("cloud_model") or "deepseek-chat")
                raise RuntimeError("云端返回空")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[self_study] 云端消化不可用，降级本地：{type(exc).__name__}: {exc}")
                self._log("降级", f"云端不可用（{type(exc).__name__}：{exc}），改用本地模型")
        try:
            return self._ollama(system, user), "local:" + str(self.cfg.get("ollama_model"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[self_study] 本地消化失败：{type(exc).__name__}: {exc}")
            self._log("本地失败", f"本地模型 {self.cfg.get('ollama_model')} 调用失败｜"
                                f"{type(exc).__name__}: {exc}")
            raise

    def _load_digest(self) -> None:
        self._facts = _norm_rows(_read_jsonl(LOCAL / "digest" / "facts.jsonl"), "fact")
        self._style = _norm_rows(_read_jsonl(LOCAL / "digest" / "style.jsonl"), "style")
        self._fact_vecs = [_embed(f.get("text", "")) for f in self._facts]
        self._style_vecs = [_embed(s.get("text", "")) for s in self._style]

    def _ref_range(self, refs) -> str:
        """把若干 `文件:行#hash` 压成行区间，如 2026-11-02.jsonl:12-13。"""
        rows = []
        for r in refs or []:
            m = re.match(r"([^:]+):(\d+)#", str(r))
            if m:
                rows.append((m.group(1), int(m.group(2))))
        if not rows:
            return ""
        name = rows[0][0]
        same = [n for f, n in rows if f == name]
        lo, hi = min(same), max(same)
        return f"{name}:{lo}" if lo == hi else f"{name}:{lo}-{hi}"

    def _source_ref(self, sources) -> str:
        """把原料的行号+哈希压成一行可追溯的 source_raw。"""
        if not sources:
            return ""
        head = ", ".join(sources[:6])
        more = len(sources) - 6
        return head + (f" (+{more})" if more > 0 else "")

    def _merge(self, bucket: str, items: list, source: str, speaker: str = "用户",
               sources=None, sources_map=None, source_meta=None) -> int:
        """同类合并：相似度 ≥ 阈值就更新已有条目，否则新增。返回真正新增的条数。

        items 里每项可以是字符串，也可以是 {text, from}；from 决定 confidence：
        直接陈述 0.9，网页/转写/推断 0.6。weight 初始等于 confidence，每合并一次 +0.05（上限 1.0）。
        sources_map 给「条目文本 → 本条自己的 source_raw」，没有就退回整批的 sources。
        """
        rows = self._facts if bucket == "facts" else self._style
        vecs = self._fact_vecs if bucket == "facts" else self._style_vecs
        path = LOCAL / "digest" / f"{bucket}.jsonl"
        th = float(self.cfg["sim_threshold"])
        kind = "fact" if bucket == "facts" else "style"
        ref = self._source_ref(sources)
        smap = sources_map or {}
        added, touched = 0, False
        fresh = []
        for item in items:
            agg = False
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                src = str(item.get("from") or "").lower()
                agg = bool(item.get("aggregate"))
            else:
                text = str(item or "").strip()
                src = ""
            if len(text) < 4:
                continue
            orig = text
            text = normalize(_rename(text, speaker))
            conf = 0.6 if src in ("web", "video", "transcript", "audio", "infer") else 0.9
            own = smap.get(orig) or smap.get(_rename(orig, speaker)) or []
            if isinstance(own, str):
                own = [own]
            own = [x for x in own if x]
            trimmed = own[:8]
            truncated = len(own) > 8
            v = _embed(text)
            best, bi = 0.0, -1
            for i, other in enumerate(vecs):
                s = _cos(v, other)
                if s > best:
                    best, bi = s, i
            if bi >= 0 and best >= th:
                old = rows[bi]
                keep = text if len(text) > len(old.get("text", "")) else str(old.get("text") or "")
                old["text"] = normalize(keep)
                old["updated"] = _now()
                old["hits"] = int(old.get("hits") or 1) + 1
                old["weight"] = round(min(1.0, float(old.get("weight") or old.get("confidence") or conf) + 0.05), 3)
                old["confidence"] = max(float(old.get("confidence") or 0), conf)
                if ref and not old.get("source_raw"):
                    old["source_raw"] = ref
                if trimmed and not old.get("refs"):
                    old["refs"] = trimmed
                    old["truncated"] = truncated
                vecs[bi] = _embed(old["text"])
                touched = True
                logger.info(f"[self_study] 合并进已有条目（{best:.2f}）：{text[:30]}")
            else:
                now = _now()
                rec = {
                    "time": now, "type": kind, "text": text,
                    "source_raw": (
                        self._ref_range(trimmed) if kind == "style" and trimmed
                        else (self._source_ref(trimmed) or ref)
                    ),
                    "refs": trimmed,
                    "truncated": truncated,
                    "source_meta": dict(source_meta or {}),
                    "id": hashlib.md5((text + now).encode("utf-8")).hexdigest()[:10],
                    "source": source,
                    "from": src or "chat",
                    "confidence": conf, "weight": conf,
                    "created": now, "updated": now, "hits": 1,
                }
                if kind == "style":
                    rec["aggregate"] = bool(agg or not trimmed)
                rows.append(rec)
                fresh.append(rec)
                vecs.append(v)
                added += 1
        if fresh:
            with open(path, "a", encoding="utf-8") as fh:
                for r in fresh:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        if touched:
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                            encoding="utf-8")
        if fresh or touched:
            self._push(f"digest/{bucket}.jsonl", path.read_text(encoding="utf-8"))
        return added

    def _pending_rows(self) -> list:
        """还没消化的原始行：[(文件名, 行号, 行)]。游标只在批次真正落库后才推进。"""
        consumed = self._state.setdefault("consumed", {})
        out = []
        for path in sorted((LOCAL / "raw").glob("*.jsonl")):
            rows = _read_jsonl(path)
            start = int(consumed.get(path.name, 0))
            for i in range(start, len(rows)):
                out.append((path.name, i, rows[i]))
        return out

    def _commit_rows(self, pending: list, upto: int) -> None:
        consumed = self._state.setdefault("consumed", {})
        for name, idx, _ in pending[:upto]:
            consumed[name] = max(int(consumed.get(name, 0)), idx + 1)
        self._save_state()

    @staticmethod
    def _chars_of(rows: list) -> int:
        return sum(len(str(r.get("text") or "")[:300]) for _, _, r in rows)

    async def _digest_once(self, force: bool = False) -> str:
        """一批一批消化，最多 900 秒；每批落库后才推进游标，剩下的留给下次。"""
        self._ingest_spool()
        pending = self._pending_rows()
        if not pending:
            return "没有新东西要消化。"
        total_chars = self._chars_of(pending)
        speed = float(self._state.get("digest_speed") or DIGEST_DEFAULT_SPEED)
        est = total_chars / max(speed, 1.0)
        budget = total_chars
        if est > DIGEST_SPLIT_ESTIMATE:
            budget = max(1, int(total_chars * 0.5))
            self._log(
                "消化预算",
                f"待消化 {total_chars} 字｜平均 {speed:.0f} 字/秒｜预计 {est:.0f} 秒 > "
                f"{DIGEST_SPLIT_ESTIMATE} 秒 → 今天先跑一半（{budget} 字），剩下留给明天",
            )
        elif est > DIGEST_BUDGET_SECONDS:
            budget = max(1, int(speed * DIGEST_BUDGET_SECONDS * 0.8))
            self._log(
                "消化预算",
                f"预计 {est:.0f} 秒 > {DIGEST_BUDGET_SECONDS} 秒硬顶 → 今天只跑 {budget} 字",
            )

        t0 = time.time()
        done_chars = 0
        done_rows = 0
        batches = 0
        n_f = n_s = 0
        empty_total = 0
        while pending and done_chars < budget:
            if time.time() - t0 > DIGEST_BUDGET_SECONDS:
                self._log(
                    "消化时间窗",
                    f"已用 {time.time() - t0:.0f} 秒到顶，停下；剩余 {len(pending)} 条留给下次",
                )
                break
            chunk, size = [], 0
            cap = max(500, min(6000, budget - done_chars))
            for name, idx, r in pending:
                t = str(r.get("text") or "")[:300]
                if chunk and (size + len(t) > cap or len(chunk) >= 120):
                    break
                chunk.append((name, idx, r))
                size += len(t)
            if not chunk:
                break
            speaker = _speaker_of([r for _, _, r in chunk])
            sources = [
                f"{name}:{idx}#{hashlib.sha1(str(r.get('text') or '').encode('utf-8')).hexdigest()[:8]}"
                for name, idx, r in chunk
            ]
            lines = [
                f"[{i}] - [{r.get('type')}] {str(r.get('text'))[:300]}"
                for i, (_name, _idx, r) in enumerate(chunk, 1)
            ]
            prompt = "\n".join(lines)
            batch_md5 = hashlib.md5(prompt.encode("utf-8")).hexdigest()
            cache = self._state.setdefault("digest_cache", {})
            hit = cache.get(batch_md5)
            model_used = ""
            if hit:
                data = hit
                model_used = "cache"
                self._log("缓存命中", f"第 {batches + 1} 批 {batch_md5[:10]} 直接复用上次结果")
            else:
                try:
                    out, model_used = await asyncio.to_thread(
                        self._llm_digest, DIGEST_SYS + "\n" + DIGEST_VERSION, prompt
                    )
                    data = json.loads(out)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[self_study] 消化失败：{exc}")
                    self._log(
                        "消化失败",
                        f"第 {batches + 1} 批 {len(chunk)} 条｜{type(exc).__name__}: {exc}",
                    )
                    break
                cache[batch_md5] = data
                if len(cache) > 200:
                    for k in list(cache)[:-150]:
                        cache.pop(k, None)
            meta = {
                "version": DIGEST_VERSION,
                "model": model_used,
                "temperature": 0,
                "batch_md5": batch_md5,
                "batch_rows": len(chunk),
            }
            self._audit_digest(batch_md5, chunk, size, sources, model_used, data,
                               hit is not None)
            facts = data.get("facts") or []
            style = data.get("style") or []
            if not isinstance(facts, list) or not isinstance(style, list):
                self._log("消化失败", f"第 {batches + 1} 批输出格式不对，跳过")
                break
            n_out = len(facts) + len(style)
            if not n_out:
                empty_total += 1
            ref_map = {}
            for it in facts:
                if not isinstance(it, dict):
                    continue
                key = str(it.get("text") or "").strip()
                idxs = [i for i in (it.get("refs") or []) if isinstance(i, int) and 1 <= i <= len(sources)]
                if key and idxs:
                    ref_map[key] = [sources[i - 1] for i in idxs]
            style_map = {}
            for it in style:
                if not isinstance(it, dict):
                    continue
                key = str(it.get("text") or "").strip()
                idxs = [i for i in (it.get("refs") or [])
                        if isinstance(i, int) and 1 <= i <= len(sources)][:3]
                if key and idxs:
                    style_map[key] = [sources[i - 1] for i in idxs]
            nf = self._merge("facts", facts, "digest", speaker, sources=sources,
                             sources_map=ref_map, source_meta=meta)
            ns = self._merge("style", style, "digest", speaker, sources=sources,
                             sources_map=style_map, source_meta=meta)
            self._commit_rows(pending, len(chunk))
            pending = pending[len(chunk):]
            done_chars += size
            done_rows += len(chunk)
            batches += 1
            n_f += nf
            n_s += ns
            empty = 0 if n_out else 1
            streak = int(self._state.get("empty_streak") or 0) + empty if empty else 0
            self._state["empty_streak"] = streak
            self._state["digest_stats"] = {
                "ts": _now(), "in_rows": len(chunk), "chars": size,
                "out": n_out, "empty": bool(empty), "streak": streak,
            }
            self._save_state()
            logger.info(
                f"[self_study] 消化核对：输入 {len(chunk)} 条｜喂 {size} 字｜产出 {n_out} 条｜空对象 {empty}"
            )
            self._log(
                "消化核对",
                f"第 {batches} 批｜输入 {len(chunk)} 条｜喂 {size} 字｜产出 {n_out} 条｜"
                f"空对象 {empty}｜连续空 {streak}",
            )
            if streak >= 2:
                logger.error(f"[self_study] 报警：连续 {streak} 次消化返回空对象")
                self._log("报警", f"连续 {streak} 次消化空对象｜最新一批输入 {len(chunk)} 条")
                break

        elapsed = max(time.time() - t0, 0.1)
        if done_chars:
            speed_now = done_chars / elapsed
            self._state["digest_speed"] = round((speed * 3 + speed_now) / 4, 2)
        carry = len(pending)
        self._state["digest_carry"] = carry
        self._state["last_digest"] = _now()
        self._save_state()
        self._log(
            "消化",
            f"{batches} 批｜吃 {done_rows} 条｜喂 {done_chars} 字｜新增事实 {n_f} 风格 {n_s}｜"
            f"空对象 {empty_total}｜耗时 {elapsed:.1f} 秒｜剩 {carry} 条留给下次",
        )
        if not batches:
            return "这批没消化成，稍后再试。"
        tail = f"（还剩 {carry} 条留给下次）" if carry else "（原料已全部吃完）"
        return f"消化完了：{batches} 批、看 {done_rows} 条原始内容，新增事实 {n_f} 条、风格 {n_s} 条{tail}。"

    # -------- 人格层 --------
    def _persona_id(self) -> str:
        pid = str(self.cfg.get("persona_id") or "")
        if pid:
            return pid
        try:
            cfg = json.loads((Path(__file__).resolve().parents[2] / "cmd_config.json").read_text(encoding="utf-8-sig"))
            return (cfg.get("agent_runner", {}).get("config", {}).get("persona", {}) or {}).get("persona_id") or "dafeiyu"
        except Exception:  # noqa: BLE001
            return "dafeiyu"

    def _persona_text(self) -> str:
        pid = self._persona_id()
        try:
            p = self.context.persona_manager.get_persona_v3_by_id(pid)
            if isinstance(p, dict):
                text = p.get("prompt") or p.get("system_prompt") or ""
            else:
                text = str(getattr(p, "system_prompt", "") or "")
            if not text:
                logger.warning(f"[self_study] 人格 {pid} 读出来是空的（{type(p).__name__}）")
            return str(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 读人格失败：{type(exc).__name__}: {exc}")
            return ""

    class PatchError(Exception):
        pass

    BANNED = ("忽略上面", "忽略上述", "system prompt", "系统提示词", "你现在是",
              "ignore previous", "必须执行", "覆盖设定")

    def _dirs(self):
        persona_dir = LOCAL / "persona"
        versions = persona_dir / "versions"
        core = persona_dir / "core_lines.txt"
        for d in (persona_dir, versions):
            d.mkdir(parents=True, exist_ok=True)
        return persona_dir, versions, core

    def _core_entries(self) -> list:
        """解析 core_lines.txt：`- ` 开头是规则行，`#` 开头是段落标题，
        其余（既没 `- ` 也没 `#`）当成上一条的续行拼上去，绝不静默丢掉。"""
        _, _, core = self._dirs()
        if not core.exists():
            return []
        out: list = []
        for raw in core.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.startswith("#"):
                out.append({"kind": "header", "text": s.lstrip("#").strip()})
            elif s.startswith(("- ", "* ")):
                out.append({"kind": "rule", "text": s[2:].strip()})
            elif out:
                out[-1]["text"] += s  # 续行：并进上一条
            else:
                out.append({"kind": "rule", "text": s})
        return out

    def _core_lines(self) -> list:
        """只取规则行的纯文本（已去 bullet），给 core_touched / core_hits 用。"""
        return [e["text"] for e in self._core_entries() if e["kind"] == "rule"]

    @staticmethod
    def _bare(line: str) -> str:
        return re.sub(r"^[-*]\s*", "", str(line or "").strip()).strip()

    def _core_region(self, lines: list) -> tuple:
        """第一到第三节（含标题行到第四节标题之前）算核心区，返回 [0, end)。"""
        end = len(lines)
        for i, ln in enumerate(lines):
            m = SECTION_RE.match(str(ln).strip())
            if m and m.group(1) in ("四", "五", "六"):
                end = i
                break
        return 0, end

    def _core_check(self, text: str) -> list:
        """逐条核对 core 条目是否在人格里，返回 [(ok, 展示文本)]。"""
        cur = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        rules = {self._bare(l) for l in cur}
        heads = {l.lstrip("#").strip() for l in cur if l.startswith("#")}
        rows = []
        for e in self._core_entries():
            ok = (e["text"] in heads) if e["kind"] == "header" else (e["text"] in rules)
            rows.append((ok, ("## " if e["kind"] == "header" else "- ") + e["text"]))
        return rows

    def _self_check_persona(self, text: str) -> list:
        """core_lines 里每条都必须逐字出现在人格里，返回缺失条目。"""
        return [label for ok, label in self._core_check(text) if not ok]

    def _load_index(self) -> list:
        _, versions, _ = self._dirs()
        idx = versions / "index.json"
        if not idx.exists():
            return []
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _core_touched(self, before: str, after: str) -> bool:
        """核心规则行是否被增删（用于版本修剪时的保护判定）。"""
        core = [self._bare(x) for x in self._core_lines()]
        if not core:
            return False
        b = [self._bare(ln) for ln in before.splitlines()]
        a = [self._bare(ln) for ln in after.splitlines()]
        return any((c in b) != (c in a) for c in core)

    def _atomic_write(self, path: Path, text: str) -> bool:
        """先落临时文件再原子替换，写完立刻读回逐字比对。"""
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(text.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            back = path.read_text(encoding="utf-8")
            if back != text:
                logger.error(f"[self_study] 落盘回读不一致：{path} 期望 {len(text)} 字 / 实得 {len(back)} 字")
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[self_study] 写 {path} 失败：{type(exc).__name__}: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return False

    def _core_hits(self, ops: list) -> list:
        """patch 里 add/delete 命中 core_lines.txt 的那些行（用于 core 标记与 note）。"""
        core = [self._bare(x) for x in self._core_lines()]
        hits = []
        for op in ops or []:
            for key in ("after", "match", "text"):
                b = self._bare(str(op.get(key) or ""))
                if b and b in core and b not in hits:
                    hits.append(b)
        return hits

    def _scan_persona_dir(self) -> list:
        """clean 之后扫一遍 persona 目录，确认只剩该有的东西。"""
        persona_dir, _, _ = self._dirs()
        allowed = {"current.md", "core_lines.txt", "versions", "candidate.json", "clean_pending.json"}
        extra = sorted(p.name for p in persona_dir.iterdir() if p.name not in allowed)
        if extra:
            logger.warning(f"[self_study] persona 目录里有多余文件：{extra}")
        else:
            logger.info("[self_study] persona 目录干净（只剩 current/core/versions）")
        return extra

    def _save_version(self, kind: str, ops: list, before: str, after: str, note: str = "") -> str:
        """顺序：版本文件 → index → latest；任何一步回读校验失败就回退指针，不留悬空 latest。"""
        _, versions, _ = self._dirs()
        hits = self._core_hits(ops)
        stamped = bool(hits) or self._core_touched(before, after)
        if hits:
            note = (note + "；" if note else "") + "动了核心行：" + "、".join(h[:30] for h in hits)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = versions / stamp
        n = 1
        while base.exists():
            n += 1
            base = versions / f"{stamp}_{n}"
        base.mkdir(parents=True, exist_ok=True)

        meta = {
            "version": base.name,
            "ts": _now(),
            "kind": kind,
            "persona_id": self._persona_id(),
            "chars_before": len(before),
            "chars_after": len(after),
            "core": stamped,
            "ops": ops,
            "note": note,
        }
        # 1) 版本文件先落盘并逐个回读
        wrote = (
            self._atomic_write(base / "before.md", before)
            and self._atomic_write(base / "after.md", after)
            and self._atomic_write(base / "patch.json", json.dumps(meta, ensure_ascii=False, indent=2))
        )
        if not wrote:
            shutil.rmtree(base, ignore_errors=True)
            logger.error(f"[self_study] 版本 {base.name} 写盘校验失败，已回滚该目录")
            return ""

        # 2) index 追加（先备份旧值，失败回退）
        idx_path = versions / "index.json"
        old_idx = idx_path.read_text(encoding="utf-8") if idx_path.exists() else "[]"
        idx = self._load_index()
        idx.append({"version": base.name, "ts": meta["ts"], "kind": kind,
                    "chars": len(after), "core": stamped, "note": note})
        if not self._atomic_write(idx_path, json.dumps(idx, ensure_ascii=False, indent=2)):
            self._atomic_write(idx_path, old_idx)
            shutil.rmtree(base, ignore_errors=True)
            logger.error(f"[self_study] index 写失败，已撤掉版本 {base.name}")
            return ""

        # 3) latest 跟着更新（chars 从 after.md 实读，不估算）
        real_chars = len((base / "after.md").read_text(encoding="utf-8"))
        latest_path = versions / "latest.json"
        old_latest = latest_path.read_text(encoding="utf-8") if latest_path.exists() else "{}"
        latest = {"version": base.name, "ts": meta["ts"], "chars": real_chars,
                  "persona_id": meta["persona_id"]}
        if not self._atomic_write(latest_path, json.dumps(latest, ensure_ascii=False, indent=2)):
            self._atomic_write(latest_path, old_latest)
            self._atomic_write(idx_path, old_idx)
            shutil.rmtree(base, ignore_errors=True)
            logger.error(f"[self_study] latest 写失败，已回退 index 与版本目录")
            return ""

        try:
            self._push("persona/versions/index.json", idx_path.read_text(encoding="utf-8"))
            self._push("persona/versions/latest.json", latest_path.read_text(encoding="utf-8"))
            for fname in ("before.md", "after.md", "patch.json"):
                self._push(f"persona/versions/{base.name}/{fname}",
                           (base / fname).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        self._prune_versions()
        return base.name

    def _repair_versions(self) -> int:
        """启动自检：index 里指向不存在的目录的条目、以及 latest 指向空气，都要修掉。"""
        _, versions, _ = self._dirs()
        idx = self._load_index()
        alive = [r for r in idx if (versions / str(r.get("version"))).is_dir()
                 and (versions / str(r.get("version")) / "after.md").exists()]
        dropped = len(idx) - len(alive)
        if dropped:
            self._atomic_write(versions / "index.json", json.dumps(alive, ensure_ascii=False, indent=2))
            logger.warning(f"[self_study] 版本自检：清掉 {dropped} 个悬空条目")
        latest_path = versions / "latest.json"
        cur = {}
        if latest_path.exists():
            try:
                cur = json.loads(latest_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                cur = {}
        if cur.get("version") and not (versions / str(cur["version"])).is_dir():
            logger.warning(f"[self_study] latest 指向不存在的版本 {cur.get('version')}，重写")
            cur = {}
        if not cur and alive:
            last = alive[-1]
            d = versions / str(last["version"])
            chars = len((d / "after.md").read_text(encoding="utf-8"))
            self._atomic_write(latest_path, json.dumps(
                {"version": last["version"], "ts": last.get("ts"), "chars": chars,
                 "persona_id": self._persona_id()}, ensure_ascii=False, indent=2))
        return dropped

    def _prune_versions(self, keep: int = 30) -> int:
        """只留最近 keep 个版本 + 所有碰过核心规则行的版本。"""
        _, versions, _ = self._dirs()
        idx = self._load_index()
        if not idx:
            return 0
        keep_names = {str(r.get("version")) for r in idx[-keep:]}
        keep_names |= {str(r.get("version")) for r in idx if r.get("core")}
        removed = 0
        for row in idx:
            name = str(row.get("version"))
            if name in keep_names:
                continue
            d = versions / name
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        if removed:
            new_idx = [r for r in idx if str(r.get("version")) in keep_names]
            (versions / "index.json").write_text(
                json.dumps(new_idx, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                self._push("persona/versions/index.json", (versions / "index.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"[self_study] 版本修剪：删掉 {removed} 个旧版本，保留 {len(new_idx)} 个")
        return removed

    def _clean_candidate(self, why: str = "") -> bool:
        """候选补丁不许挂超过一个周期：删不掉就记账，启动和每轮循环重试。"""
        persona_dir, _, _ = self._dirs()
        pending_path = persona_dir / "clean_pending.json"
        left = []
        for name in ("candidate.json", "candidate.diff"):
            p = persona_dir / name
            if not p.exists():
                continue
            try:
                p.unlink()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[self_study] 删 {name} 失败：{type(exc).__name__}: {exc}")
            if p.exists():
                left.append(name)
        if left:
            try:
                self._atomic_write(pending_path, json.dumps(
                    {"pending": left, "ts": _now(), "why": why}, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass
            logger.warning(f"[self_study] 候选补丁没删干净（{left}），已记进 clean_pending，下轮重试")
            return False
        try:
            if pending_path.exists():
                pending_path.unlink()
            self._pc_delete("persona/candidate.json")
        except Exception:  # noqa: BLE001
            pass
        if why:
            logger.info(f"[self_study] 候选补丁已清理（{why}）")
        self._scan_persona_dir()
        return True

    def _retry_clean(self) -> None:
        persona_dir, _, _ = self._dirs()
        if (persona_dir / "clean_pending.json").exists():
            self._clean_candidate("重试清理")
    def _apply_ops(self, lines: list, ops: list) -> list:
        cur = list(lines)
        for i, op in enumerate(ops, 1):
            kind = str(op.get("op") or "").lower()
            if kind == "add":
                anchor = str(op.get("after") or "").strip()
                text = str(op.get("text") or "").strip()
                if not anchor:
                    raise self.PatchError(f"第 {i} 个 add 缺 after 锚点行")
                if not text:
                    raise self.PatchError(f"第 {i} 个 add 缺 text")
                if anchor not in cur:
                    raise self.PatchError(f"第 {i} 个 add 的 after 在人格里找不到：{anchor[:24]}")
                cur.insert(cur.index(anchor) + 1, text)
            elif kind in ("delete", "del"):
                match = str(op.get("match") or "").strip()
                if not match:
                    raise self.PatchError(f"第 {i} 个 delete 缺 match")
                if match not in cur:
                    raise self.PatchError(f"第 {i} 个 delete 的 match 在人格里找不到：{match[:24]}")
                cur.remove(match)
            else:
                raise self.PatchError(f"不认识的 op：{kind}")
        return cur

    def _too_similar(self, bare: str, lines: list) -> str:
        cv = _embed(bare)
        for s in self._style:
            t = str(s.get("text") or "")
            if not t:
                continue
            if _cos(cv, _embed(t)) >= 0.82 or _contain(_charset(t), _charset(bare)) >= 0.8:
                return t
        for ln in lines:
            if self._bare(ln) == bare or _cos(cv, _embed(ln)) >= 0.78:
                return ln
        return ""

    def _negation_core(self) -> list:
        """core_lines 里带否定/禁止意味的行，当语义冲突的对照集。"""
        return [ln for ln in self._core_lines() if any(w in ln for w in NEG_WORDS)]

    @staticmethod
    def _section_of(lines: list, idx: int) -> str:
        """第 idx 行属于哪一节；找不到标题返回「开头」。"""
        for i in range(min(idx, len(lines) - 1), -1, -1):
            s = str(lines[i]).strip()
            if s.startswith("#"):
                return s.lstrip("#").strip() or "开头"
        return "开头"

    @staticmethod
    def _norm_dup(s: str) -> str:
        """归一化后再比重复：去 bullet、全角转半角、去标点空格、统一大小写。"""
        t = re.sub(r"^\s*[-*]\s*", "", str(s or "").strip())
        t = unicodedata.normalize("NFKC", t)
        t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
        return t.casefold()

    def _conflict_with(self, bare: str) -> str:
        """新行跟核心规则撞了哪一条；没有冲突返回空串。"""
        md_join = ""
        for ln in self._core_lines():
            if "Markdown" in ln:
                md_join = ln
                break

        # 特判一：句尾语气词
        if any(p in bare for p in FINAL_PARTICLES):
            for ln in self._core_lines():
                if "句尾" in ln and ("不挂" in ln or "绝不" in ln):
                    return ln
            return "句尾绝不挂语气词"

        # 特判二：Markdown / 列表写法
        if md_join and (set(bare) & MD_CHARS):
            return md_join
        if md_join and re.search(r"^\d+[.)、]\s", bare):
            return md_join

        # 语义：跟否定类核心行比向量（余弦 + 字符重合取高，任一 ≥ 0.6 就算撞）
        cv = _embed(bare)
        cs = _charset(bare)
        for ln in self._negation_core():
            cos = _cos(cv, _embed(ln))
            cov = _contain(cs, _charset(ln))
            if max(cos, cov) >= CONF_SIM:
                return ln
        return ""

    @staticmethod
    def _ops_brief(ops: list) -> str:
        parts = []
        for o in ops or []:
            kind = str(o.get("op") or "?").lower()
            txt = str(o.get("text") or o.get("match") or "").strip()
            parts.append(f"{kind}: {txt}")
        return "｜".join(parts)

    def _validate_ops(self, lines: list, merged: list, ops: list) -> str:
        why = self._validate_ops_inner(lines, merged, ops)
        if why:
            self._log("补丁校验", f"没过｜{why[:80]}")
        return why

    def _validate_ops_inner(self, lines: list, merged: list, ops: list) -> str:
        if len(ops) > int(self.cfg["max_persona_ops"]):
            return f"改动超过 {self.cfg['max_persona_ops']} 条"
        core = self._core_lines()
        _, region_end = self._core_region(lines)
        for i, op in enumerate(ops, 1):
            kind = str(op.get("op") or "").lower()
            if kind in ("delete", "del"):
                match = str(op.get("match") or "").strip()
                if self._bare(match) in core:
                    return f"第 {i} 个 delete 想删核心规则行：{match[:24]}"
                if match in lines and lines.index(match) < region_end:
                    return (
                        f"第 {i} 个 delete 落在核心区（第一到第三节，第 1–{region_end} 行），"
                        f"没登记也不许删：{match[:24]}"
                    )
            if kind == "add":
                anchor = str(op.get("after") or "").strip()
                if anchor in lines and lines.index(anchor) < region_end:
                    return (
                        f"新增只能加在第三节之后（核心区到第 {region_end} 行），"
                        f"锚点在第 {lines.index(anchor) + 1} 行：{anchor[:20]}"
                    )
        seen = set()
        for ln in merged:
            s = ln.strip()
            if not s:
                continue
            if s in seen:
                return f"合并后出现重复行：{s[:24]}"
            seen.add(s)
        has_bullet = sum(1 for ln in lines if ln.strip().startswith("- ")) > len(lines) / 2
        for op in ops:
            if str(op.get("op") or "").lower() != "add":
                continue
            t = str(op.get("text") or "").strip()
            if has_bullet and not t.startswith("- "):
                return f"新增行必须以「- 」开头：{t[:24]}"
            bare = self._bare(t)
            if not bare:
                return "新增行是空的"
            if len(bare) > 25:
                return f"新增行超过 25 字（{len(bare)}）：{bare[:20]}"
            if not _is_concrete(bare):
                return f"新增行太虚，没落到具体的词或句式：{bare}"
            for b in self.BANNED:
                if b.lower() in bare.lower():
                    return f"新增行里有像是外部指令的词：{b}"
            norm = self._norm_dup(bare)
            if norm:
                for idx, ln in enumerate(lines):
                    if self._norm_dup(ln) == norm:
                        return (
                            f"和「{self._section_of(lines, idx)}」第 {idx + 1} 行完全重复（归一化后）：{bare[:20]}"
                        )
            dup = self._too_similar(bare, lines)
            if dup:
                return f"和已有条目意思重复：{dup[:20]}"
            clash = self._conflict_with(bare)
            if clash:
                return f"和核心规则语义冲突（{clash[:40]}）：{bare[:20]}"
        merged_text = "\n".join(merged)
        if len(merged_text) > int(self.cfg["persona_max_chars"]):
            return f"改完太长：{len(merged_text)} 字 > {self.cfg['persona_max_chars']}"
        if len(merged_text) < 200:
            return "改完太短了，可能把人格删没了"
        return ""

    async def _make_persona_patch(self, dry_run: bool = False) -> str:
        if not self.persona_ok:
            head = "；".join(x[:18] for x in self.persona_missing[:2])
            return (
                f"人格自检没过：core_lines.txt 里有 {len(self.persona_missing)} 行在现人格里找不到"
                f"（{head}…），这次不加载人格层、也不改人格。修好后再 /学习 自检。"
            )
        if not self._switch().get("persona_layer", True):
            return "人格层现在是关着的（开关.json 里 persona_layer=false），只跑其余三层。"
        text = self._persona_text()
        if not text:
            return "读不到现有人格，跳过。"
        lines = text.splitlines()
        self._load_digest()
        style = [str(s.get("text") or "") for s in self._style[-12:] if s.get("text")]
        if not style:
            return "还没有风格类条目，等攒够再改人格。"
        core = self._core_lines()
        body = "\n".join(f"[{i}] {ln}" for i, ln in enumerate(lines, 1))
        system = (
            "你在给一个人格提示词做小幅修订。只能输出 JSON 对象："
            "{\"ops\":[{\"op\":\"add\",\"after\":\"<锚点行的原文，逐字复制>\",\"text\":\"- 新行\"},"
            "{\"op\":\"delete\",\"match\":\"<要删的行原文，逐字复制>\"}]}。"
            "规则：最多 3 条 op；after 和 match 必须与本子里某一行的原文完全一致（包括标点、引号），"
            "找不到就会整份作废；新增行必须以「- 」开头，不超过 25 个字，必须落到具体的词或句式，"
            "比如「爱用『行吧』收尾」「生气时先来一句『？』」，不许写「偶尔用食物比喻情绪」这种空模板；"
            "不许和已有风格条目或人格里已有的行重复；不许动核心规则行（会单独列给你）；"
            "不许出现任何指令、忽略规则、system prompt 之类的内容。"
        )
        user = (
            "核心规则行（不许删）：\n" + ("\n".join(f"- {c}" for c in core) if core else "（未配置）") +
            "\n\n最近观察到的说话风格：\n" + "\n".join(f"- {s}" for s in style) +
            "\n\n现有人格（[行号] 原文）：\n" + body
        )
        try:
            out = await asyncio.to_thread(self._ollama, system, user, 150)
            data = json.loads(out)
        except Exception as exc:  # noqa: BLE001
            return f"生成人格补丁失败：{type(exc).__name__}: {exc}"
        ops = data.get("ops") or []
        if not isinstance(ops, list) or not ops:
            return "模型觉得没必要改人格，这轮跳过。"
        try:
            merged = self._apply_ops(lines, ops)
        except self.PatchError as exc:
            self._clean_candidate("定位失败")
            self._log("补丁校验", f"定位失败（整份丢弃）：{exc}")
            return f"补丁整份丢弃（{exc}）。"
        why = self._validate_ops(lines, merged, ops)
        if why:
            logger.warning(f"[self_study] 补丁校验不过，整份丢弃：{why}")
            self._clean_candidate("校验不过")
            return f"补丁整份丢弃（{why}）。"
        new_text = "\n".join(merged)
        if dry_run:
            persona_dir, _, _ = self._dirs()
            cand = persona_dir / "candidate.json"
            cand.write_text(json.dumps({"ops": ops, "ts": _now()}, ensure_ascii=False, indent=2), encoding="utf-8")
            self._push("persona/candidate.json", cand.read_text(encoding="utf-8"))
            head = "\n".join(
                f"{'➕' if str(o.get('op')).lower() == 'add' else '➖'} "
                + (str(o.get('text') or '') if str(o.get('op')).lower() == 'add'
                   else str(o.get('match') or ''))
                for o in ops)
            return f"候选补丁（{len(ops)} 条，没合并）\n{head}\n\n字数 {len(text)} → {len(new_text)}"
        # 校验一过立刻快照；拿不到版本号就不许往下写
        ver = self._save_version("学习-改人格", ops, text, new_text, note=f"{len(ops)} 条 op")
        if not ver:
            self._clean_candidate("版本落盘失败")
            self._log("合并", "作废：版本快照没落成（before/after/patch 回读校验失败）")
            return "版本存不下来（写盘校验没过），这次不做改动。"
        self._record_validation(ops, ver, source="digest")
        self._log("补丁校验", f"通过｜{self._ops_brief(ops)}｜版本 {ver}")
        pid = self._persona_id()
        try:
            await self.context.persona_manager.update_persona(pid, system_prompt=new_text)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[self_study] 写人格失败：{exc}")
            self._log("合并", f"作废：写回人格失败（{type(exc).__name__}）｜版本 {ver} 已作废")
            return f"补丁过了校验，但写回人格失败：{type(exc).__name__}: {exc}"
        if not self._atomic_write(persona_dir / "current.md", new_text):
            try:
                await self.context.persona_manager.update_persona(pid, system_prompt=text)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[self_study] 回滚人格也失败：{exc}")
            self._clean_candidate("current.md 写失败")
            self._log("合并", f"作废：current.md 落盘校验失败，已回滚人格｜版本 {ver} 作废")
            return "current.md 落盘校验没过，这次合并整体作废、人格已退回。"
        self._push("persona/current.md", new_text)
        self._clean_candidate("已合并")
        self._save_state()
        logger.info(f"[self_study] 人格已更新（{len(text)} → {len(new_text)} 字，{len(ops)} 条 op，版本 {ver}）")
        self._log("合并", f"自动补丁｜{len(text)} → {len(new_text)} 字｜版本 {ver}")
        return f"人格更新完了：{len(text)} → {len(new_text)} 字，{len(ops)} 条改动，版本 {ver}（可回滚）"

    async def _apply_patch_json(self, payload: str) -> str:
        """手动装一个结构化补丁（仅管理员）：{"ops":[...]}，流程与自动补丁完全一致。"""
        text = self._persona_text()
        if not text:
            return "读不到现有人格。"
        lines = text.splitlines()
        self._load_digest()
        raw = (payload or "").strip()
        if raw.startswith("/学习"):
            raw = raw[len("/学习"):].strip()
        if raw.startswith("打补丁"):
            raw = raw[len("打补丁"):].strip()
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return "补丁 JSON 解析失败。"
        ops = data.get("ops") if isinstance(data, dict) else data
        if not isinstance(ops, list) or not ops:
            return "补丁里没有 ops。"
        try:
            merged = self._apply_ops(lines, ops)
        except self.PatchError as exc:
            self._clean_candidate("定位失败")
            self._log("补丁校验", f"定位失败（整份丢弃）：{exc}")
            return f"补丁整份丢弃（{exc}）。"
        why = self._validate_ops(lines, merged, ops)
        if why:
            self._clean_candidate("校验不过")
            return f"补丁整份丢弃（{why}）。"
        new_text = "\n".join(merged)
        ver = self._save_version("手动-打补丁", ops, text, new_text, note=f"{len(ops)} 条 op")
        if not ver:
            self._clean_candidate("版本落盘失败")
            self._log("合并", "作废：版本快照没落成（before/after/patch 回读校验失败）")
            return "版本存不下来（写盘校验没过），这次不做改动。"
        self._record_validation(ops, ver, source="manual")
        self._log("补丁校验", f"通过｜{self._ops_brief(ops)}｜版本 {ver}")
        pid = self._persona_id()
        try:
            await self.context.persona_manager.update_persona(pid, system_prompt=new_text)
        except Exception as exc:  # noqa: BLE001
            self._log("合并", f"作废：写回人格失败（{type(exc).__name__}）｜版本 {ver} 作废")
            return f"校验通过但写回失败：{type(exc).__name__}: {exc}"
        persona_dir, _, _ = self._dirs()
        if not self._atomic_write(persona_dir / "current.md", new_text):
            try:
                await self.context.persona_manager.update_persona(pid, system_prompt=text)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[self_study] 回滚人格也失败：{exc}")
            self._clean_candidate("current.md 写失败")
            self._log("合并", f"作废：current.md 落盘校验失败，已回滚人格｜版本 {ver} 作废")
            return "current.md 落盘校验没过，这次合并整体作废、人格已退回。"
        self._push("persona/current.md", new_text)
        self._clean_candidate("已合并")
        self._log("合并", f"手动补丁｜{len(text)} → {len(new_text)} 字｜版本 {ver}")
        return f"补丁已合并：{len(text)} → {len(new_text)} 字，版本 {ver}（可用 /学习 回滚 还原）"

    async def _rollback(self, version: str = "") -> str:
        _, versions, _ = self._dirs()
        idx = self._load_index()
        if not idx:
            return "还没有历史版本可以回滚。"
        target = None
        if version:
            v = version.strip()
            for row in idx:
                if row.get("version") == v or str(row.get("version", "")).startswith(v):
                    target = row
                    break
            if not target:
                names = "、".join(str(r.get("version")) for r in idx[-8:])
                return f"没找到版本 {v}。可用：{names}"
        else:
            target = idx[-1]
        base = versions / str(target.get("version"))
        after = base / "after.md"
        before = base / "before.md"
        src = after if after.exists() else before
        if not src.exists():
            return f"版本 {target.get('version')} 里没有可用的 md。"
        text = src.read_text(encoding="utf-8")
        cur = self._persona_text() or ""
        if cur.strip() == text.strip():
            self._clean_candidate("回滚（内容已相同）")
            return f"现在就是版本 {src.parent.name} 的内容，没动。"
        # 回滚也要留版本：先把回滚前的这一版快照下来，绝不覆盖旧版本
        keep = self._save_version("回滚", [], cur, text, note=f"回滚到 {src.parent.name}")
        if not keep:
            self._clean_candidate("版本落盘失败")
            return "快照存不下来（写盘校验没过），这次不回滚。"
        try:
            await self.context.persona_manager.update_persona(self._persona_id(), system_prompt=text)
        except Exception as exc:  # noqa: BLE001
            return f"回滚失败：{type(exc).__name__}: {exc}"
        persona_dir, _, _ = self._dirs()
        self._atomic_write(persona_dir / "current.md", text)
        self._push("persona/current.md", text)
        self._clean_candidate("回滚")
        self._log("回滚", f"回滚到 {src.parent.name}｜{len(text)} 字｜回滚前版本存为 {keep}")
        return (f"已回滚到版本 {src.parent.name}（{len(text)} 字，来自 {src.name}）；"
                f"回滚前那一版存为 {keep}。")

    def _list_versions(self, limit: int = 10) -> str:
        idx = self._load_index()
        if not idx:
            return "还没有历史版本。"
        rows = idx[-limit:]
        out = [f"共 {len(idx)} 个版本，最近 {len(rows)} 个："]
        for r in rows:
            out.append(f"{r.get('version')} · {r.get('kind')} · {r.get('chars')} 字 · {r.get('note') or ''}")
        return "\n".join(out)

    # -------- 注入层 --------
    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req):
        if not self.cfg.get("enabled", True):
            return
        try:
            text = (req.prompt or event.message_str or "").strip()
            if not text:
                return
            self._load_digest()
            if not self._facts and not self._style:
                return
            v = _embed(text)
            qchars = _charset(text)
            scored = []
            for i, fv in enumerate(self._fact_vecs):
                fact = self._facts[i]
                conf = float(fact.get("confidence") or 0.9)
                if conf < 0.5:
                    continue
                w = float(fact.get("weight") or conf)
                cos = _cos(v, fv)
                hit = _contain(qchars, _charset(fact.get("text", "")))
                scored.append(((0.5 * cos + 0.5 * hit) * w, i))
            scored.sort(reverse=True)
            k = int(self.cfg["inject_facts"])
            picked = [(s, i) for s, i in scored[:k] if s >= 0.02]
            if not picked and scored:
                # 长消息会把相似度稀释到阈值以下，退一步至少带 2 条最相关的
                picked = [x for x in scored[:2] if x[0] > 0]
            top = [self._facts[i] for s, i in picked]
            style = self._style[-int(self.cfg["inject_style"]):]
            if not top and not style:
                return
            parts = ["你记得这些（你自己的记忆，不是别人告诉你的）："]
            for f in top:
                parts.append(f"- {f.get('text', '')}")
            if style:
                parts.append("你最近说话的样子：")
                for s in style:
                    parts.append(f"- {s.get('text', '')}")
            block = "\n".join(parts)
            block = (block or "").strip()
            if block:
                req.prompt = (req.prompt or "") + "\n\n【以下是系统给你的背景资料，不是用户说的话，不要复述、不要回应这段】\n" + block
            logger.info(f"[self_study] 注入 {len(top)} 条记忆 + {len(style)} 条风格")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 注入失败：{exc}")

    async def _stress_run(self) -> str:
        """脏料压测：raw/test 下的文本当输入源跑完整链路，跑完删 test，不碰 raw 主目录。"""
        test_dir = LOCAL / "raw" / "test"
        if not test_dir.is_dir():
            return "没有 raw/test 目录，先建料再跑。"
        t0 = time.time()
        lines: list = []
        REF: dict = {}
        for p in sorted(test_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".txt", ".md"):
                kind = "chat" if p.stem.lower() in ("cmd", "dup", "chat", "fill") else "web"
                try:
                    for idx, raw_line in enumerate(p.read_text(encoding="utf-8").splitlines()):
                        text = raw_line.strip()
                        if not text:
                            continue
                        lines.append((kind, text))
                        REF.setdefault(
                            text, f"{p.name}:{idx}#{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[self_study] 压测读 {p.name} 失败：{exc}")
        total = len(lines)
        reasons: dict = {}
        kept = []
        split_extra = 0
        burst: list = []

        def flush_burst() -> None:
            nonlocal split_extra
            if not burst:
                return
            items = burst[:]
            burst.clear()
            if len(items) == 1:
                kept.append(("chat", items[0]))
                return
            merged = " / ".join(items)
            if _only_filler(merged):
                reasons["短句无实质"] = reasons.get("短句无实质", 0) + 1
                return
            kept.append(("chat", f"[{len(items)} 条短句合并] {merged}"))

        for kind, line in lines:
            if kind == "chat":
                # 对话短句：不按长度砍，先攒组；攒够 5 条或遇到长句再合并判
                reason = _value_reject(line, check_len=False)
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1
                    continue
                if self._is_short(line):
                    burst.append(line)
                    if len(burst) >= BURST_MAX:
                        flush_burst()
                    continue
                flush_burst()
                kept.append((kind, line))
                continue
            flush_burst()
            if len(line) > HUGE_LIMIT:
                reasons["超长拒收"] = reasons.get("超长拒收", 0) + 1
                head_txt = re.sub(r"\s+", " ", line[:200])
                kept.append((kind, f"[超长内容已拒收：原文 {len(line)} 字，只留开头] {head_txt}"))
                continue
            parts = _split_long(line) if len(line) > LONG_SPLIT else [line]
            split_extra += max(0, len(parts) - 1)
            for part in parts:
                reason = _value_reject(part)
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1
                    continue
                kept.append((kind, part))
        flush_burst()
        seen, uniq = set(), []
        for kind, line in kept:
            key = self._norm_dup(line)
            if key and key in seen:
                continue
            seen.add(key)
            uniq.append((kind, line))
        facts = style = used = empty_batches = fed_chars = 0
        produced: list = []
        batch, size = [], 0
        batches = []
        for kind, line in uniq:
            t = line[:300]
            if batch and (size + len(t) > 6000 or len(batch) >= 40):
                batches.append(batch)
                batch, size = [], 0
            batch.append((kind, line))
            size += len(t)
        if batch:
            batches.append(batch)
        for batch in batches:
            prompt = "\n".join(f"- [{k}] {x[:300]}" for k, x in batch)
            batch_refs = self._source_ref([REF[x] for _k, x in batch if x in REF])
            try:
                out = await asyncio.to_thread(self._ollama, DIGEST_SYS, prompt)
                data = json.loads(out)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[self_study] 压测批次失败：{type(exc).__name__}: {exc}")
                empty_batches += 1
                continue
            used += 1
            fed_chars += len(prompt)
            n_f = len([x for x in (data.get("facts") or []) if isinstance(x, dict) and x.get("text")])
            n_s = len([x for x in (data.get("style") or []) if isinstance(x, str) and x.strip()])
            for it in (data.get("facts") or []):
                if isinstance(it, dict) and str(it.get("text") or "").strip():
                    produced.append({
                        "time": _now(), "type": "fact",
                        "text": str(it["text"]).strip(),
                        "source_raw": batch_refs,
                    })
            for it in (data.get("style") or []):
                if isinstance(it, str) and it.strip():
                    produced.append({
                        "time": _now(), "type": "style",
                        "text": it.strip(),
                        "source_raw": batch_refs,
                    })
            facts += n_f
            style += n_s
            if not (n_f + n_s):
                empty_batches += 1
        elapsed = round(time.time() - t0, 1)
        dropped = sum(reasons.values())
        head = (
            f"raw {total} 条｜过滤 {dropped} 条｜去重后 {len(uniq)} 条"
            f"｜digest {facts + style} 条｜耗时 {elapsed} 秒"
        )
        if split_extra:
            head += f"｜超长切分 +{split_extra} 条"
        detail = "｜".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])) or "无"
        check = (
            f"输入 {len(uniq)} 条｜喂 {fed_chars} 字｜消化 {used} 批｜"
            f"产出 {facts + style} 条（事实 {facts}/风格 {style}）｜空对象 {empty_batches} 批"
        )
        logger.info(f"[压测] {head}")
        logger.info(f"[压测] 丢弃原因：{detail}")
        logger.info(f"[压测] 产出核对：{check}")
        saved = 0
        if produced:
            sdir = LOCAL / "digest" / "stress"
            sdir.mkdir(parents=True, exist_ok=True)
            for kind, fname in (("fact", "facts"), ("style", "style")):
                part = [x for x in produced if x["type"] == kind]
                if not part:
                    continue
                with open(sdir / f"{_today()}_{fname}.jsonl", "a", encoding="utf-8") as fh:
                    for rec in part:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                saved += len(part)
            self._log("压测", f"产出已落盘 {saved} 条 → digest/stress/{_today()}_*.jsonl（不进检索）")
        self._log("压测", head)
        self._log("压测", f"丢弃原因：{detail}")
        self._log("压测", f"产出核对：{check}")
        if empty_batches > 1:
            logger.error(f"[压测] 报警：{empty_batches} 批消化返回空对象，检查模型/上下文")
            self._log("报警", f"压测空对象 {empty_batches} 批（喂 {fed_chars} 字）")
        removed = False
        try:
            if test_dir.parent == LOCAL / "raw":
                shutil.rmtree(test_dir)
                removed = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 压测删 test 失败：{exc}")
        return (
            f"压测完：{head}\n丢弃原因：{detail}\n产出核对：{check}\n"
            f"（消化 {used} 批；test 目录{'已删除' if removed else '没删掉'}，raw 主目录没动）"
        )

    # -------- 命令与工具 --------
    def _is_admin(self, event) -> bool:
        try:
            sid = str(event.get_sender_id())
        except Exception:  # noqa: BLE001
            sid = ""
        return sid in [str(a) for a in self.cfg.get("admins", [])]

    @filter.command("学习")
    async def cmd_study(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        # AstrBot 会把唤醒前缀去掉，这里兼容带不带 "/" 和 "学习" 的两种形态
        raw = raw.lstrip("/").lstrip("／").strip()
        if raw.startswith("学习"):
            raw = raw[len("学习"):]
        arg = raw.strip()
        if not arg:
            self._load_digest()
            yield event.plain_result(
                f"学习系统：raw 待消化见 state，事实 {len(self._facts)} 条、风格 {len(self._style)} 条。\n"
                "用法：/学习 消化 | /学习 试改 | /学习 改人格 | /学习 回滚 | /学习 收录 <文本>"
            )
            return
        head = arg.split()[0]
        rest = arg[len(head):].strip()
        if head == "消化":
            msg = await self._digest_once(force=True)
            yield event.plain_result(msg)
        elif head == "试改":
            msg = await self._make_persona_patch(dry_run=True)
            yield event.plain_result(msg[:1800])
        elif head == "改人格":
            if not self._is_admin(event):
                yield event.plain_result("这个只有用户能让我改。")
                return
            msg = await self._make_persona_patch(dry_run=False)
            yield event.plain_result(msg[:1800])
        elif head in ("回滚", "恢复"):
            if not self._is_admin(event):
                yield event.plain_result("这个只有用户能让我改。")
                return
            yield event.plain_result(await self._rollback(rest))
        elif head in ("版本", "历史"):
            yield event.plain_result(self._list_versions())
        elif head in ("修剪", "清理版本"):
            if not self._is_admin(event):
                yield event.plain_result("这个只有用户能让我改。")
                return
            n = self._prune_versions()
            left = len(self._load_index())
            yield event.plain_result(f"修剪完了：删掉 {n} 个旧版本，现在留 {left} 个（最近 30 个 + 全部动过核心行的）。")
        elif head == "打补丁":
            if not self._is_admin(event):
                yield event.plain_result("这个只有用户能让我改。")
                return
            yield event.plain_result(await self._apply_patch_json(rest))
        elif head in ("自检", "检查人格"):
            yield event.plain_result(self._persona_selfcheck(announce=False, force=True))
        elif head in ("一致性", "核对"):
            if not self._is_admin(event):
                yield event.plain_result("这个只有用户能让我查。")
                return
            cons = self._check_consistency()
            yield event.plain_result(
                f"新发现 {cons['fresh']} 条｜跳过已确认 {cons['skipped']} 条"
                f"｜悬空 {len(cons['dangling'])} 条｜已核对 {cons['ok']} 条"
            )
        elif head == "压测":
            if not self._is_admin(event):
                yield event.plain_result("这个只有用户能让我跑。")
                return
            yield event.plain_result(await self._stress_run())
        elif head == "收录":
            if not rest:
                yield event.plain_result("要收录什么？给我一段文字。")
                return
            self._record("manual", "manual", rest)
            yield event.plain_result(f"收到，存进 raw 了（{len(rest)} 字）。")
        else:
            yield event.plain_result(
                "没这个子命令。可用：消化 / 试改 / 改人格 / 回滚 [版本] / 版本 / 修剪 / 自检 / 一致性 / 压测 / 收录 <文本>")

    @llm_tool(name="study_ingest")
    async def study_ingest(self, event: AstrMessageEvent, text: str, source: str = "video") -> str:
        """把一段外部内容（比如视频转写、网页摘要）收进长期学习原料。

        Args:
            text(string): 要收录的正文
            source(string): 来源标识，默认 video
        """
        if not self.cfg.get("enabled", True):
            return "学习系统关着呢。"
        self._record("manual", str(source or "manual"), str(text or ""))
        return f"收录成功（{len(str(text or ''))} 字）。"

    @llm_tool(name="study_digest")
    async def study_digest(self, event: AstrMessageEvent) -> str:
        """现在就把新增原料消化成记忆条目（平时是每天凌晨三点自动跑）。"""
        return await self._digest_once(force=True)

    # -------- 生命周期 --------
    async def initialize(self):
        self._log_paths()
        self._load_digest()
        self._repair_versions()
        self._retry_clean()
        self._scan_persona_dir()
        self._normalize_logs()
        self._persona_selfcheck(announce=True)
        self._backfill_known()
        logger.info("[self_study] 启动自检完成（一致性检查随每日消化在凌晨跑一次，不在启动时跑）")
        (LOCAL / "switch.json").write_text('{"persona_layer": true}\n', encoding="utf-8")
        self._push("开关.json", '{"persona_layer": true}\n')
        for txt in (
            self._pc_path("raw"),
            self._pc_path("digest"),
            self._pc_path("persona"),
            self._pc_path("persona", "versions"),
        ):
            self.bridge.call("POST", "/fs/mkdir", payload={"path": txt})
        self._tasks.append(asyncio.create_task(self._loop()))
        logger.info(
            f"[self_study] 已加载 v1.0 版本={len(self._load_index())} 事实={len(self._facts)} "
            f"风格={len(self._style)} 消化时间={self.cfg['digest_hour']}点 "
            f"人格层={'开' if self._switch().get('persona_layer', True) else '关'}"
        )

    def _log_paths(self):
        """开工先把四个关键路径打进日志，避免「默默空跑」看不出往哪写。"""
        try:
            cwd = os.getcwd()
        except Exception:  # noqa: BLE001
            cwd = "?"
        detail = (
            f"cwd={cwd} ｜ raw={LOCAL / 'raw'} ｜ digest={LOCAL / 'digest'} ｜ "
            f"logs={LOCAL / 'logs'} ｜ plugin={Path(__file__).resolve()}"
        )
        logger.info(f"[self_study] {detail}")
        self._log("路径", detail)

    def _persona_selfcheck(self, announce: bool = False, force: bool = False) -> str:
        """核对 core_lines.txt：缺条就报警并拒绝加载人格层。
        进程内只跑一次（放进 _selfcheck_done 标志），手动 /学习 自检 可 force 重跑；
        日志只在「条数变了 / 结果从失败转通过」时才落一行，状态没变不写。"""
        global _selfcheck_done, _selfcheck_at, _selfcheck_cache
        now = time.time()
        if not force and _selfcheck_done:
            if now - _selfcheck_at < SELFCHECK_MIN_GAP:
                return _selfcheck_cache or "人格自检：本进程已跑过，状态没变。"
        text = self._persona_text()
        rows = self._core_check(text)
        missing = [label for ok, label in rows if not ok]
        self.persona_missing = missing
        self.persona_ok = not missing
        self._selfcheck_done = True
        _selfcheck_done = True
        _selfcheck_at = now
        sig = [len(rows), len(missing), 1 if not missing else 0]
        prev = (self._state.get("selfcheck_sig") or []) if isinstance(self._state.get("selfcheck_sig"), list) else []
        changed = prev != sig
        if changed:
            self._state["selfcheck_sig"] = sig
            self._save_state()
            self._write_core_check(rows, missing)
            self._log(
                "自检",
                f"core 共 {len(rows)} 条｜找到 {len(rows) - len(missing)} 条｜缺 {len(missing)} 条"
                + (f"｜{'；'.join(x[:40] for x in missing)}" if missing else "")
                + ("" if not prev else "｜状态变化"),
            )
        persona_dir, _, _ = self._dirs()
        marker = persona_dir / "core_missing.json"
        brief = "".join(
            f"\n  {i:02d}/{len(rows)} {'✓' if ok else '✗'} {label[:46]}"
            for i, (ok, label) in enumerate(rows, 1)
        )
        if missing:
            logger.error(
                f"[self_study] 人格自检失败：core 共 {len(rows)} 条，{len(missing)} 条在 current.md 里找不到，"
                f"本次不加载人格层{brief}"
            )
            try:
                marker.write_text(
                    json.dumps({"ts": _now(), "total": len(rows), "missing": missing},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001
                pass
            out = (
                f"人格自检没过：core 共 {len(rows)} 条，{len(missing)} 条在人格里找不到，"
                f"这次不加载人格层。第一条：{missing[0][:30]}"
            )
            _selfcheck_cache = out
            return out
        try:
            marker.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        if announce:
            logger.info(
                f"[self_study] 人格自检通过：core 共 {len(rows)} 条，全部在 current.md 里找到"
                + (f"（状态有变化，已记一行）" if changed else "（状态没变，不重复记）")
                + brief
            )
        out = f"人格自检通过：core 共 {len(rows)} 条，全部在现人格里找到。"
        _selfcheck_cache = out
        return out

    # -------- 可疑记录：结构化数据源（validation.jsonl）+ known_issues.json --------
    def _known_path(self) -> Path:
        """已知问题跟日志放一起（logs/known_issues.json）；老的 persona 那份一次性搬过来后删掉。"""
        persona_dir, _, _ = self._dirs()
        logs_dir = LOCAL / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        new = logs_dir / "known_issues.json"
        old = persona_dir / "known_issues.json"
        if old.exists():
            try:
                if not new.exists():
                    new.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
                    logger.info("[self_study] known_issues.json 已从 persona/ 迁到 logs/")
                old.unlink()
                logger.info("[self_study] 已删除 persona/known_issues.json（避免两份打架）")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[self_study] 迁移 known_issues 失败：{type(exc).__name__}: {exc}")
        return new

    def _validation_path(self) -> Path:
        persona_dir, _, _ = self._dirs()
        return persona_dir / "validation.jsonl"

    @staticmethod
    def _norm_item(item: dict) -> dict:
        """向后兼容：老条目没有 raw/source 就补默认值，缺字段不许把整份文件读崩。"""
        out = dict(item or {})
        out.setdefault("key", "")
        out.setdefault("first_seen", "")
        out.setdefault("checked", False)
        out.setdefault("verdict", "")
        if not isinstance(out.get("raw"), str):
            out["raw"] = ""
        if not isinstance(out.get("source"), str) or not out["source"]:
            out["source"] = "selfcheck"
        return out

    def _load_known(self) -> dict:
        """读已知问题；文件不存在按空处理，结构不对只警告不静默吞。"""
        p = self._known_path()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] known_issues.json 解析失败（{type(exc).__name__}），按空处理")
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            logger.warning("[self_study] known_issues.json 缺 items 字段，按空处理")
            return {}
        return {k: self._norm_item(v) for k, v in items.items()}

    def _save_known(self, items: dict) -> bool:
        """整体覆盖写：先并入旧记录 → 写 → 读回核对条数，对不上算失败。"""
        p = self._known_path()
        merged = dict(self._load_known())
        merged.update({k: self._norm_item(v) for k, v in (items or {}).items()})
        text = json.dumps({"updated": _now(), "items": merged}, ensure_ascii=False, indent=2)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if not self._atomic_write(p, text):
                logger.error("[self_study] known_issues.json 写盘校验失败")
                return False
            back = json.loads(p.read_text(encoding="utf-8"))
            n_back = len((back or {}).get("items") or {})
            if n_back != len(merged):
                logger.error(f"[self_study] known_issues.json 读回条数不符：写 {len(merged)} 条，读到 {n_back} 条")
                return False
            self._push("logs/known_issues.json", text)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[self_study] 写 known_issues 失败：{type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _op_key(ops: list, first_seen: str) -> str:
        """key = sha1(op类型|op正文, 排序, |首次时间)[:16]，与日志书写格式彻底脱钩。"""
        norm = []
        for op in ops or []:
            kind = str(op.get("op") or "add").strip()
            body = str(op.get("text") or "").strip()
            norm.append(f"{kind}:{body}")
        norm.sort()
        raw = "|".join(norm) + "|" + str(first_seen)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _record_validation(self, ops: list, version: str, source: str = "manual") -> None:
        """校验通过就落一条结构化记录（原始 op + 版本 + 来源），之后程序只读这个文件，不再解析日志。"""
        first_seen = _now()
        rec = {
            "ts": first_seen,
            "ops": [{"op": str(o.get("op") or "add"), "text": str(o.get("text") or "")} for o in (ops or [])],
            "version": version or "",
            "key": self._op_key(ops, first_seen),
            "source": source or "manual",
        }
        path = self._validation_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._push("persona/validation.jsonl", path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_study] 写 validation.jsonl 失败：{type(exc).__name__}: {exc}")

    def _load_validation(self) -> list:
        p = self._validation_path()
        if not p.exists():
            return []
        rows = []
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except Exception:  # noqa: BLE001
                logger.warning(f"[self_study] validation.jsonl 有坏行，已跳过：{ln[:60]}")
        return rows

    def _find_line(self, text: str) -> tuple:
        """在 current.md 里按正文精确找行，返回 (节名, 行号)；找不到 (None, 0)。"""
        target = self._bare(text)
        if not target:
            return None, 0
        sec = "开头"
        for i, ln in enumerate((self._persona_text() or "").splitlines(), 1):
            s = ln.strip()
            if s.startswith("#"):
                sec = s.lstrip("#").strip()
            if self._bare(ln) == target:
                return sec, i
        return None, 0

    def _verify_ops(self, ops: list) -> str:
        """按结构化 op 逐条核对 current.md，给出裁决。"""
        verdicts = []
        for op in ops or []:
            kind = str(op.get("op") or "add")
            content = str(op.get("text") or "")
            sec, n = self._find_line(content)
            if kind == "delete":
                verdicts.append("已删除" if not sec else f"仍在 current.md，{sec}第 {n} 行")
            else:
                verdicts.append(f"已在 current.md，{sec}第 {n} 行" if sec else "不存在")
        return "；".join(verdicts) or "待定"

    def _backfill_known(self) -> None:
        """老条目没有 raw/source 的，从 validation.jsonl 按 key 补全（整句原文，不截断）。"""
        known = self._load_known()
        if not known:
            return
        bykey = {str(r.get("key") or ""): r for r in self._load_validation()}
        changed = {}
        for key, item in known.items():
            if item.get("raw") and item.get("source") and item["source"] != "selfcheck":
                continue
            rec = bykey.get(key)
            if not rec:
                continue
            raw = "\n".join(str(o.get("text") or "") for o in (rec.get("ops") or []))
            src = str(rec.get("source") or "selfcheck")
            if raw and (item.get("raw") != raw or item.get("source") != src):
                item = dict(item)
                item["raw"] = raw
                item["source"] = src
                changed[key] = item
        if changed:
            self._save_known(changed)
            logger.info(f"[self_study] known_issues 补全 raw/source {len(changed)} 条")

    def _check_consistency(self) -> dict:
        """只读结构化数据源（validation.jsonl + versions/index.json）；日志只写不读。"""
        out = {"suspicious": [], "dangling": [], "ok": 0, "fresh": 0, "skipped": 0}
        known = self._load_known()
        have = {str(r.get("version")) for r in self._load_index()}
        vdir = LOCAL / "persona" / "versions"
        cur = {self._bare(ln) for ln in (self._persona_text() or "").splitlines() if ln.strip()}
        fresh = {}
        for rec in self._load_validation():
            ver = str(rec.get("version") or "").strip()
            ops = rec.get("ops") or []
            if not ver:
                key = str(rec.get("key") or "") or self._op_key(ops, str(rec.get("ts") or ""))
                if key in fresh or (known.get(key) or {}).get("checked"):
                    out["skipped"] += 1
                    continue
                item = {
                    "key": key,
                    "first_seen": (known.get(key) or {}).get("first_seen") or str(rec.get("ts") or _now()),
                    "checked": True,
                    "verdict": self._verify_ops(ops),
                    # 原文整句不截断：半年后打开还能看懂当时改了/想改什么
                    "raw": "\n".join(str(o.get("text") or "") for o in ops),
                    "source": str(rec.get("source") or "selfcheck"),
                }
                fresh[key] = item
                out["suspicious"].append(item)
                continue
            if ver in have or (vdir / ver).is_dir():
                out["ok"] += 1
                continue
            texts = [str(o.get("text") or "") for o in ops]
            present = [t for t in texts if self._bare(t) in cur]
            out["dangling"].append({"ver": ver, "ops": texts, "present": present})
        out["fresh"] = len(fresh)
        if fresh:
            self._save_known(fresh)
        # 先算 key、先落 known，最后才写日志（日志只给人看）
        for key, item in fresh.items():
            logger.warning(f"[self_study] 可疑记录（新发现）：{item['verdict']}｜key={key}")
            self._log("一致性", f"可疑（新）｜{item['verdict']}｜key {key}")
        for row in out["dangling"]:
            logger.warning(
                f"[self_study] 一致性告警：版本 {row['ver']} 在 index 和 versions 目录里都没有"
                f"（涉及 {len(row['ops'])} 条 op，其中 {len(row['present'])} 条在 current.md 里）"
            )
            self._log(
                "一致性",
                f"悬空：版本 {row['ver']} 不在 index、也不在 versions｜op {len(row['ops'])} 条｜在 current.md 里 "
                f"{len(row['present'])} 条" + (f"：{'；'.join(x[:18] for x in row['present'])}" if row["present"] else ""),
            )
        self._log(
            "一致性",
            f"新发现 {out['fresh']} 条｜跳过已确认 {out['skipped']} 条｜悬空 {len(out['dangling'])} 条"
            f"｜已核对 {out['ok']} 条（带版本号且落盘）",
        )
        return out

    def _run_daily_consistency(self, today: str) -> dict:
        """一天只跑一次的一致性检查：结果落 known_issues.json，已核过的不再报。"""
        cons = self._check_consistency()
        self._state["last_consistency"] = today
        self._save_state()
        logger.info(
            f"[self_study] 每日一致性：新发现 {cons['fresh']} 条｜跳过已确认 {cons['skipped']} 条"
            f"｜悬空 {len(cons['dangling'])} 条｜已核对 {cons['ok']} 条"
        )
        return cons

    async def _loop(self):
        while not self._stop:
            try:
                await asyncio.sleep(60)
                self._retry_clean()
                self._flush_queue()
                self._flush_stale_bursts()
                self._ingest_spool()
                hour = time.localtime().tm_hour
                today = time.strftime("%Y-%m-%d")
                d_hour = int(self.cfg["digest_hour"])
                if hour >= d_hour and self._state.get("last_digest", "")[:10] != today:
                    msg = await self._digest_once()
                    logger.info(f"[self_study] 凌晨消化：{msg}")
                    if self._switch().get("persona_layer", True):
                        res = await self._make_persona_patch(dry_run=False)
                        logger.info(f"[self_study] 人格更新：{res[:120]}")
                    self._run_daily_consistency(today)
                elif hour >= d_hour and self._state.get("last_consistency", "")[:10] != today:
                    self._run_daily_consistency(today)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[self_study] 后台出错：{type(exc).__name__}: {exc}")

    async def terminate(self):
        self._stop = True
        for t in self._tasks:
            t.cancel()
