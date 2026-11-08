# 常见问题

## 装了之后她的 token 消耗没降下来？

先确认三件事：

1. `context_budget` 是否真的在跑 —— 日志里应该出现
   `[context_budget] 已加载｜高水位 ... 字`
2. 有没有触发过裁剪 —— `[context_budget] 上下文 X 字 > Y｜裁掉 N 条 → Z 字`
3. 命中率怎么看 —— 在数据库里查 `provider_stats` 表的 `token_input_cached` 与 `token_input_other`：

```sql
SELECT created_at,
       token_input_other  AS miss,
       token_input_cached AS hit,
       token_input_cached * 100.0 / (token_input_other + token_input_cached) AS hit_rate
FROM provider_stats
ORDER BY rowid DESC LIMIT 10;
```

命中率长期低于 50%，说明还有东西在改前缀（见下一条）。

## 命中率一直是 3%，为什么？

因为**前缀每轮都变**。按发生概率排序：

1. 有插件在往 `system_prompt` 末尾追加「每轮都变」的内容（最致命）
2. 框架的按轮压缩在重写历史头部
3. 你改了人格文件，或注入了带时间戳的东西

排查方法：把当前 system prompt 落盘，看末尾有没有动态内容：

```python
# 在任意 on_llm_request 钩子里
Path("last_system.txt").write_text(req.system_prompt, encoding="utf-8")
```

如果末尾出现「你记得这些…」「现在是 X 点」之类，就是它。

## 她说「不记得以前的事」

`recall_history` 依赖两个东西：

- `archive/` 里有没有归档文件（`ls plugin_data/astrbot_plugin_recall_history/archive`）
- live 库里的对话是否被框架压缩吃掉了

首次安装时归档是空的，需要时间积累；也可以用脚本从旧的 `data_v4.db.bak_*` 备份里补录。

## 记忆注入量多少合适？

总预算 **≤1500 字**。超了就变成噪声：

- 事实类 5 条（约 300 字）
- 风格类 3 条（约 150 字）
- 跨会话旁线 10 条（约 700 字）

如果你的模型上下文只有 32k，把这几项都减半。

## Windows 上路径反斜杠报错？

插件全部用 `pathlib` 和 `os.path.join` 拼接，不硬编码分隔符。
如果你自己写配置，注意 JSON 里反斜杠要写成 `\\`。

## 为什么不用向量数据库？

1 万条以内，字符 bigram 词袋 + 余弦足够快（实测 5000 条约 0.1–0.3 秒），
而且零依赖、零运维。超过 10 万条再考虑引入向量库。

## 情感记忆为什么默认关着？

因为**它现在还不合格**：实测 97 条「记忆」里 96 条是把用户原话又存了一遍
（其中一条是「你还记得我们最早聊的是什么吗」——那是提问，不是记忆）。

正确做法是只记「事件 + 感受」，等这一层重新设计完成再打开。

## 数据都存在哪？会不会上传？

默认全部本地：

- 会话流水：`plugin_data/astrbot_plugin_memory_vault/index.jsonl`
- 提炼记忆：`plugin_data/astrbot_plugin_self_study/digest/`
- 归档：`plugin_data/astrbot_plugin_recall_history/archive/`

只有你显式配置了 `bridge_url` 或 `WHALE_BRIDGE`，才会往你自己的电脑推一份。
没有任何云端上传逻辑。
