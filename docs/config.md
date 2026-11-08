# 配置说明

Whale Mind 的每个插件都能独立开关，配置有三层来源，**后者覆盖前者**：

1. 代码里的 `DEFAULTS`
2. 插件配置：`<AstrBot>/data/plugin_data/<插件名>/config.json`
3. 环境变量（仅部分字段，用于部署时覆盖）

插件在 `<AstrBot>/data/plugins/<插件名>/`，所有默认路径都相对于插件位置推算，**不含任何硬编码绝对路径**。

---

## context_budget（最重要）

`plugin_data/astrbot_plugin_context_budget/config.json`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关 |
| `high_chars` | int | `60000` | 超过这个字符数才裁剪 |
| `low_chars` | int | `40000` | 一次裁到这个字符数 |
| `keep_last` | int | `8` | 最近多少条永不裁掉 |
| `strip_images` | bool | `true` | 是否剥掉老消息里的图片 |
| `image_keep` | int | `2` | 最近几条消息保留图片 |
| `log` | bool | `true` | 每次裁剪写日志 |

调参建议：

- 你的模型上下文 32k → `high_chars 24000 / low_chars 16000`
- 128k 上下文 → 保持默认
- 1M 上下文（如 DeepSeek）→ `high_chars 120000 / low_chars 80000`，否则历史太短她会健忘

> 高水位与低水位之间的差值就是「缓冲」。差值越大，裁剪频率越低，缓存越稳；
> 但差值过大会导致一次裁剪掉太多，她的短期记忆出现断层。建议差值 = 高水位的 1/3。

---

## recall_history

`plugin_data/astrbot_plugin_recall_history/config.json` 目前为空，行为由环境变量控制：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WHALE_DB` | `<AstrBot>/data/data_v4.db` | AstrBot 数据库路径 |
| `WHALE_PLUGIN_DATA` | 插件的 plugin_data 目录 | 归档放哪 |
| `WHALE_BRIDGE` | 空 | 可选，把归档推到本机（如 `http://127.0.0.1:8899`） |
| `WHALE_BRIDGE_TOKEN` | 空 | 本机桥的鉴权 token |
| `WHALE_HISTORY_DIR` | 空 | 归档推到本机的目标目录 |

不配 `WHALE_BRIDGE` 就完全本地，不会往外发任何东西。

---

## memory_vault

`plugin_data/astrbot_plugin_memory_vault/config.json`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `bridge_url` | string | `http://127.0.0.1:8899` | 本机桥地址（不填则不镜像） |
| `token` | string | `""` | 桥的鉴权 token |
| `own_folder` | string | `""` | 镜像到哪个目录（空 = 只存本地） |
| `subdir` | string | `记忆` | 子目录名 |
| `flush_seconds` | int | `60` | 落盘间隔 |
| `max_lines` | int | `5000` | 索引最多保留多少行 |
| `admins` | list | `[]` | 允许手动触发同步的用户 ID |

---

## self_study

`plugin_data/astrbot_plugin_self_study/config.json`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `bridge_url` | string | `http://127.0.0.1:8899` | 本机桥 |
| `token` | string | `""` | 桥 token |
| `pc_root` | string | `""` | 镜像目录（空 = 只存本地） |
| `ollama_url` | string | `http://127.0.0.1:11434` | 本地模型地址 |
| `ollama_model` | string | `qwen3:8b-16k` | 本地模型名 |
| `digest_use_cloud` | bool | `true` | 优先用云端模型消化 |
| `cloud_model` | string | `deepseek-chat` | 云端模型名 |
| `admins` | list | `[]` | 允许执行 `/学习` 命令的用户 |
| `digest_hour` | int | `3` | 每天几点消化 |
| `inject_facts` | int | `5` | 每轮注入几条事实 |
| `inject_style` | int | `3` | 每轮注入几条风格 |
| `persona_max_chars` | int | `4200` | 人格文件上限 |
| `max_persona_ops` | int | `3` | 单次自改人格最多几行 |

云端模型需要 `cmd_config.json` 里有可用的 provider；没有就自动降级到 ollama。

---

## emotion_memory（默认关闭）

`plugin_data/astrbot_plugin_emotion_memory/config.json`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `shadow` | bool | `true` | 影子模式：只记不注入 |
| `enabled` | bool | `false` | 是否真的写入 |
| `min_len` | int | `8` | 短于这个长度不记 |
| `skip_questions` | bool | `true` | 疑问句不记（提问不是记忆） |
| `max_len` | int | `500` | 单条截断 |
| `stats_log` | bool | `true` | 定期打印统计 |

---

## global_memory

`plugin_data/astrbot_plugin_global_memory/config.json`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 开关 |
| `max_entries` | int | `300` | 全局时间线保留多少条 |
| `inject_count` | int | `10` | 每次注入几条旁线消息 |
| `ttl_minutes` | int | `180` | 超过多久的不再注入 |
| `max_chars` | int | `700` | 注入总字数上限 |
| `exclude_same_session` | bool | `true` | 当前会话自身不重复注入 |
| `exclude_platforms` | list | `["webchat"]` | 排除的平台（测试会话别污染） |

---

## 总预算

所有注入加起来建议 **≤ 1500 字**：

```
context_budget: 不注入（只裁剪）
self_study:     5 事实 + 3 风格  ≈ 600 字
global_memory:  10 条旁线       ≈ 700 字
emotion_memory: 0（影子模式）
```

超了就会从「帮助」变成「干扰」——模型会被无关内容带偏。
