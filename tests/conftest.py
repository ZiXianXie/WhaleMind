"""测试夹具：在没有 AstrBot 的情况下加载插件。

思路：插件只依赖 astrbot.api 的几个入口（Star / llm_tool / register / filter /
event 类型），把它们用最小桩件顶住，然后按文件路径 importlib 加载真实的
main.py。这样测的是**仓库里的源码本身**，而不是复制出来的副本。
"""

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "plugins"


class _Logger:
    def __init__(self, name="test"):
        self.name = name

    def _noop(self, *args, **kwargs):
        return None

    debug = info = warning = warn = error = exception = critical = _noop


class _Filter:
    """任何 @filter.xxx(...) 装饰器都原样返回函数。

    `filter.EventMessageType.ALL` 在真实框架里是枚举，这里用命名空间代替，
    否则模块级装饰器一执行就 AttributeError。
    """

    EventMessageType = types.SimpleNamespace(
        ALL="ALL", GROUP_MESSAGE="GROUP_MESSAGE", PRIVATE_MESSAGE="PRIVATE_MESSAGE"
    )
    EventMessageType.__dict__["ALL"] = "ALL"

    def __call__(self, *args, **kwargs):
        # llm_tool(name=...) 是直接调用桩件本身
        return self.__getattr__("__call__")(*args, **kwargs)

    def __getattr__(self, name):
        def deco(*args, **kwargs):
            if args and callable(args[0]) and len(args) == 1 and not kwargs:
                return args[0]

            def inner(func):
                return func

            return inner

        return deco


class _Star:
    def __init__(self, context=None, config=None):
        self.context = context
        self.name = "test"


class _Context:
    def __init__(self):
        self.provider_manager = types.SimpleNamespace()
        self.conversation_manager = types.SimpleNamespace()


@dataclass
class TextPart:
    text: str = ""


@dataclass
class ProviderRequest:
    prompt: str = ""
    system_prompt: str = ""
    contexts: list = field(default_factory=list)
    model: str = ""


def _install_astrbot_stub():
    if "astrbot.api" in sys.modules:
        return

    api = types.ModuleType("astrbot.api")
    api.logger = _Logger("astrbot")
    api.llm_tool = _Filter()
    api.filter = _Filter()

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = _Context
    star_mod.Star = _Star
    star_mod.register = lambda *a, **kw: (lambda cls: cls)
    api.star = star_mod

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    event_mod.filter = api.filter
    api.event = event_mod

    provider_mod = types.ModuleType("astrbot.api.provider")
    provider_mod.ProviderRequest = ProviderRequest
    provider_mod.TextPart = TextPart
    api.provider = provider_mod

    root = types.ModuleType("astrbot")
    root.api = api
    root.__path__ = []  # 让 astrbot.core.* 这种子包也能被导入

    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    agent = types.ModuleType("astrbot.core.agent")
    agent.__path__ = []
    message_mod = types.ModuleType("astrbot.core.agent.message")
    message_mod.TextPart = TextPart
    agent.message = message_mod
    core.agent = agent

    sys.modules.update({
        "astrbot": root,
        "astrbot.api": api,
        "astrbot.api.star": star_mod,
        "astrbot.api.event": event_mod,
        "astrbot.api.provider": provider_mod,
        "astrbot.core": core,
        "astrbot.core.agent": agent,
        "astrbot.core.agent.message": message_mod,
    })


def load_plugin(name: str):
    """按目录名加载 plugins/<name>/main.py，返回模块对象。"""
    _install_astrbot_stub()

    plugin_dir = PLUGINS / name
    main_py = plugin_dir / "main.py"
    assert main_py.is_file(), f"找不到 {main_py}"

    # 有些插件会 import 同目录的模块（例如 textnorm）
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))

    mod_name = f"wm_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(mod_name, main_py)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
