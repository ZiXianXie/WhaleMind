# 测试

`python -m pytest` 一条命令跑完全部用例，不依赖任何第三方库（除了 pytest 自己）。

## 为什么能脱离 AstrBot 跑

`conftest.py` 伪造了插件真正用到的那几个入口：

| 桩件 | 作用 |
| --- | --- |
| `astrbot.api.logger` | 吃掉所有日志调用 |
| `astrbot.api.star` | `Star` / `Context` / `register` 的最小替身 |
| `llm_tool` | 既可 `@llm_tool(name="x")`，也可直接调用 |
| `filter` | 任意 `@filter.xxx(...)` 都返回原函数，并带 `EventMessageType` 命名空间 |
| `TextPart` / `ProviderRequest` | 数据类替身，含 `astrbot.core.agent.message` 别名 |
| `load_plugin()` | 按文件路径 importlib 加载 `plugins/<name>/main.py` |

被测的是**仓库里的源码本身**，不是复制出来的副本——所以源码改了，测试立刻能感知。

## 覆盖了什么

54 个用例，集中在"纯函数 + 决策逻辑"这类最容易出错、又最难靠手点验证的地方：

- `test_context_budget.py`（8）：不超高不动、超高裁到低水位、保留最新消息、
  滞回（裁完不立刻再裁）、剥老图留新图、`keep_last` 保护、空上下文、多模态文本提取。
- `test_recall_history.py`（13）：中英混切词、片段截取与省略号、CRLF 折叠、
  无命中时回退到最近记录、`<system_reminder>` 剥离、非文本 part 忽略、
  配置钳制（99→23、-5→0、1e9→8000）、空串不覆盖、垃圾入参不崩。
- `test_self_study.py`（26）：采集价值判定（空行/纯符号/命令/正经句/长自然句/
  `docker logs`/heredoc/乱码）、长文切分、日志时间戳规范化、称呼替换不自指、
  语气词判定、句末标点补齐。
- `test_memory_guards.py`（7）：会话标签、哪些内容不该进记忆库（短句/命令/问句/纯表情）。

## 已知未覆盖

- 真实 AstrBot 生命周期（插件加载、事件分发、WebUI 配置注入）——靠部署后的人工冒烟。
- 网络调用（云端消化、桥推送、Ollama）——这层用伪引擎跑，不打真网。
- 星图前端（`packages/dsh-memory-galaxy`）——JS 侧暂无测试。
