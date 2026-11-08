# dsh-whale-memory

在 DeepSeek Harness 侧栏放一张「记忆星图」，把 Whale Mind 的记忆可视化。

- **侧栏图标**：点击后在主面板打开星图
- **数据来源**：读取本机记忆导出文件（默认 `~/whale-mind/galaxy.json`，可用 `WHALE_GALAXY_FILE` 覆盖）
- **零依赖**：纯前端渲染，不发额外请求

## 安装

把整个目录放进 DSH 的插件目录：

```
~/.dsh/plugins/dsh-whale-memory/
```

重启 DSH，左侧栏会出现一个鲸鱼图标。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `WHALE_GALAXY_FILE` | `~/whale-mind/galaxy.json` | 记忆星图数据文件 |
| `WHALE_PANEL_TITLE` | `记忆星图` | 侧栏与面板标题 |

数据文件格式见仓库根目录 `examples/galaxy-demo.json`（字段：`stars[] / messages[] / meta`）。

## 许可

MIT，见仓库根目录 LICENSE。
