# 第三方组件与出处

Whale Mind 自身以 MIT 许可发布。以下组件来源于第三方项目，按各自的原始许可使用与分发。

## 1. affective-memory

- **来源**：https://github.com/mirageyuza/affective-memory
- **许可**：MIT
- **用途**：`plugins/astrbot_plugin_emotion_memory/vendor/emotion_memory/` 为其核心库的 vendored 副本，
  提供情感权重、遗忘曲线与记忆震荡唤起。
- **修改说明**：
  - 接入时**强制使用 `view="original"`**，永不调用 `apply_pollution`（其默认的「情感滤镜」会按单字替换中文，
    实测会把「差不多就这样」改写成「还有改进余地不多」）。
  - `recall()` 的时间参数由「秒」换算为「小时」，并关闭随机噪声，否则同样的查询每次返回不同结果。
  - 关闭了守恒/熵/电容/流形/湍流/三相/分身等实验性后处理。

## 2. dsh-Memory-Galaxy

- **来源**：https://github.com/leesingx/dsh-Memory-Galaxy
- **许可**：MIT（见 `packages/dsh-memory-galaxy/LICENSE`）
- **用途**：`packages/dsh-memory-galaxy/` 为记忆星图的视觉原型。
- **修改说明**：
  - 数据源改为读取 `memory_vault/index.jsonl`，并把最近 800 条对话渲染为消息点。
  - 配色改为继承宿主主题 token（`--dsw-alias-*`），修正深色模式下「一片白」与容器高度塌陷问题。

## 3. 字体 / 图标

- `packages/dsh-whale-memory/icon.svg` 为本项目自绘，MIT。

---

如发现遗漏的第三方代码，请开 issue，我们会补上出处。
