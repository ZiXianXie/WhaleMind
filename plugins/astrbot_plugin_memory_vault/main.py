"""把记忆和备忘录落盘到用户电脑的 <记忆根目录>，重装插件能读回来。

结构（电脑上）：
  <记忆根目录>\\index.jsonl         —— 全量对话索引（每行一条，JSON）
  <记忆根目录>\\memo.md            —— 主备忘录（她那本 memory 插件的 memo 镜像）
  <记忆根目录>\\profile.md         —— 用户画像镜像
  <记忆根目录>\\users\\<uid>\\*.md  —— 每个用户的 soul/profile/memo/history_index

服务器上：
  /AstrBot/data/plugin_data/astrbot_plugin_memory_vault/index.jsonl     本地主副本
  /AstrBot/data/plugin_data/astrbot_plugin_memory_vault/restored/      启动时从电脑拉回来的副本
"""

import asyncio
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_memory_vault"
TAG = f"[{PLUGIN_NAME}]"
DATA_ROOT = pathlib.Path(__file__).resolve().parents[2] / "plugin_data"
CFG_FILE = DATA_ROOT / PLUGIN_NAME / "config.json"
LOCAL_DIR = DATA_ROOT / PLUGIN_NAME
LOCAL_INDEX = LOCAL_DIR / "index.jsonl"
RESTORED = LOCAL_DIR / "restored"
MEM_DIR = DATA_ROOT / "astrbot_plugin_memory"

DEFAULT_CFG = {
    "enabled": True,
    "bridge_url": "http://127.0.0.1:8899",
    "token": "",
    "own_folder": "",
    "subdir": "记忆",
    "flush_seconds": 60,
    "max_lines": 5000,
}


def _cfg(extra: dict | None = None) -> dict:
    """优先级：默认值 < plugin_data/config.json < WebUI 配置。"""
    cfg = dict(DEFAULT_CFG)
    try:
        cfg.update(json.loads(CFG_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        pass
    if isinstance(extra, dict):
        cfg.update({k: v for k, v in extra.items() if k in DEFAULT_CFG and v is not None})
    return cfg


class Bridge:
    def __init__(self, cfg: dict):
        self.base = str(cfg["bridge_url"]).rstrip("/")
        self.token = str(cfg.get("token") or "")
        self.timeout = 20

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = self.base + path
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = {"X-Diary-Token": self.token}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": f"HTTP {exc.code}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@register(PLUGIN_NAME, "Whale Mind contributors", "把记忆/备忘录/对话索引落盘到本机目录，重装可恢复", "1.0.0", "")
class MemoryVaultPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.cfg = _cfg(config or {})
        self.bridge = Bridge(self.cfg)
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        RESTORED.mkdir(parents=True, exist_ok=True)
        self.vault = str(self.cfg.get("own_folder") or "").rstrip("\\") + "\\" + str(self.cfg.get("subdir") or "记忆")
        self.lines: list[dict] = []
        self.dirty = False
        self.last_flush = 0.0
        self.last_error = ""
        self.flushed_total = 0
        self._load_local()
        self._task = asyncio.get_event_loop().create_task(self._loop())
        self._boot = asyncio.get_event_loop().create_task(self._boot_restore())

    async def _boot_restore(self):
        """启动 20 秒后从电脑拉一次记忆，插件重装后自动恢复。"""
        await asyncio.sleep(20)
        try:
            msg = await self._restore()
            logger.info(f"{TAG} {msg}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{TAG} 启动恢复失败：{exc}")

    # ---------- 本地主副本 ----------
    def _load_local(self) -> None:
        try:
            for raw in LOCAL_INDEX.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if raw:
                    self.lines.append(json.loads(raw))
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"{TAG} 本地索引 {len(self.lines)} 条")

    def _save_local(self) -> None:
        keep = self.lines[-int(self.cfg.get("max_lines", 5000)):]
        self.lines = keep
        try:
            LOCAL_INDEX.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in keep) + "\n",
                                   encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{TAG} 本地索引写失败：{exc}")

    # ---------- 启动恢复 ----------
    async def _restore(self) -> str:
        """从电脑把记忆拉回来。插件重装后第一次跑就靠这个。"""
        got = []
        missing = []
        RESTORED.mkdir(parents=True, exist_ok=True)
        for name in ("index.jsonl", "memo.md", "profile.md"):
            res = await asyncio.to_thread(self.bridge.call, "GET", "/fs/read",
                                          {"path": f"{self.vault}\\{name}", "max": 200000})
            if res.get("ok") and res.get("text"):
                (RESTORED / name).write_text(str(res["text"]), encoding="utf-8")
                got.append(name)
            elif res.get("ok") is False and ("not found" in str(res.get("error", "")) or "not a file" in str(res.get("error", ""))):
                missing.append(name)
            else:
                missing.append(f"{name}({res.get('error')})")
        if "index.jsonl" in got:
            rows = []
            for raw in (RESTORED / "index.jsonl").read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except Exception:  # noqa: BLE001
                    continue
            if len(rows) > len(self.lines):
                self.lines = rows
                self._save_local()
        if got:
            return "从电脑恢复：" + "、".join(got) + (f"（还缺 {'、'.join(missing)}）" if missing else "")
        if missing and all("not found" in x for x in missing):
            return "电脑上还没有记忆文件（第一次运行，正常）"
        return "没读到：" + "、".join(missing)

    # ---------- 记录 ----------
    def _record(self, umo: str, who: str, text: str, bot: bool) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.lines.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "umo": umo,
            "who": who,
            "bot": bot,
            "text": text[:500],
        })
        self.dirty = True

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_in(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True):
            return
        self._record(event.unified_msg_origin, str(event.get_sender_name() or event.get_sender_id()),
                     event.message_str, bot=False)

    @filter.on_decorating_result()
    async def on_out(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True):
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        parts = []
        for comp in result.chain:
            if comp.__class__.__name__ == "Plain":
                parts.append(getattr(comp, "text", ""))
        if parts:
            self._record(event.unified_msg_origin, "助手", "".join(parts), bot=True)

    # ---------- 同步 ----------
    def _push_text(self, rel: str, text: str) -> tuple[bool, str]:
        path = f"{self.vault}\\{rel}"
        res = self.bridge.call("POST", "/fs/write", None,
                               {"path": path, "content": text, "mode": "overwrite"})
        if not res.get("ok"):
            return False, str(res.get("error"))
        if not res.get("verified"):
            return False, "回读校验没通过"
        return True, f"{res.get('bytes')} 字节"

    async def _flush(self, force: bool = False) -> tuple[bool, str]:
        if not self.dirty and not force:
            return True, "没有新内容"
        self._save_local()
        payload = "\n".join(json.dumps(x, ensure_ascii=False) for x in self.lines) + "\n"
        ok, detail = await asyncio.to_thread(self._push_text, "index.jsonl", payload)
        if ok:
            self.dirty = False
            self.flushed_total += 1
            self.last_flush = time.time()
            self.last_error = ""
            return True, f"索引 {len(self.lines)} 条已落盘（{detail}）"
        self.last_error = detail
        return False, detail

    async def _mirror_memory(self) -> str:
        """把她那本 memory 插件的 memo/profile/soul 同步到电脑。"""
        pushed = []
        if not MEM_DIR.exists():
            return "没找到 memory 插件的数据目录"
        for user_dir in MEM_DIR.iterdir():
            if not user_dir.is_dir():
                continue
            uid = user_dir.name
            for fname in ("memo.md", "profile.md", "soul.md", "history_index.md", "todo.md"):
                src = user_dir / fname
                if not src.exists():
                    continue
                text = src.read_text(encoding="utf-8", errors="replace")
                ok, detail = await asyncio.to_thread(self._push_text, f"users\\{uid}\\{fname}", text)
                if ok:
                    pushed.append(f"{uid}/{fname}")
                else:
                    pushed.append(f"{uid}/{fname} 失败:{detail}")
            if fname == "memo.md" or True:
                memo = user_dir / "memo.md"
                if memo.exists():
                    await asyncio.to_thread(self._push_text, "memo.md",
                                            memo.read_text(encoding="utf-8", errors="replace"))
        return "记忆镜像：" + ("、".join(pushed) if pushed else "没有可同步的文件")

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(int(self.cfg.get("flush_seconds", 60)))
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{TAG} 落盘循环异常：{exc}")
                await asyncio.sleep(30)

    # ---------- 工具 ----------
    @llm_tool(name="vault_sync")
    async def vault_sync(self, event: AstrMessageEvent) -> str:
        """把记忆、备忘录和对话索引强制同步一份到用户电脑的 <记忆根目录>。

        Args: 无参数。
        """
        ok, detail = await self._flush(force=True)
        mirror = await self._mirror_memory()
        if ok:
            return f"同步好了：{detail}；{mirror}"
        return f"同步失败：{detail}；{mirror}"

    @llm_tool(name="vault_status")
    async def vault_status(self, event: AstrMessageEvent) -> str:
        """查看记忆落盘的状态：本地索引多少条、上次落盘时间、有没有失败。

        Args: 无参数。
        """
        age = int(time.time() - self.last_flush) if self.last_flush else -1
        lines = [
            f"本地索引：{len(self.lines)} 条",
            f"落盘次数：{self.flushed_total}，上次成功：{age} 秒前" if age >= 0 else "落盘次数：0（还没成功过）",
            f"未落盘改动：{'有' if self.dirty else '无'}",
            f"最近错误：{self.last_error or '无'}",
            f"目标目录：{self.vault}",
        ]
        return "\n".join(lines)

    @llm_tool(name="vault_restore")
    async def vault_restore(self, event: AstrMessageEvent) -> str:
        """从用户电脑把记忆拉回服务器（插件重装后用它恢复）。

        Args: 无参数。
        """
        msg = await self._restore()
        return f"{msg}；现在本地索引 {len(self.lines)} 条"

    async def terminate(self):
        try:
            self._task.cancel()
        except Exception:  # noqa: BLE001
            pass
        await self._flush(force=True)
