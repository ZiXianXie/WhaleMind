# tools/

**这些是维护者用的辅助脚本，不参与插件运行。** 全部在 Windows 上验证过
（演示实例启动 AstrBot、CDP 驱动 Edge 截图），Linux 上需要自己改路径与浏览器可执行文件。

| 脚本 | 作用 |
|---|---|
| `seed_demo.py` / `seed_demo_conv.py` | 往演示实例灌入**完全虚构**的会话、流水、事实与风格 |
| `make_galaxy_demo.py` | 生成星图演示数据 `examples/galaxy-demo.json` |
| `demo_password.py` | 把演示实例的面板密码重置为 `demo / Demo123456` |
| `shots.py` / `shot_click.py` | 用 CDP 驱动无头 Edge 截面板图（登录态靠 `localStorage.token`） |
| `make_report.py` | 把 `reports/junit.xml` 渲染成单文件 HTML 测试报告 |

## 演示实例

脚本默认读环境变量 `ASTRBOT_HOME`（默认 `<repo>/.demo/astrbot`），指向一个**独立**的
AstrBot 实例目录——不要指向你自己的生产实例。

```powershell
# 1. 准备一个干净的 AstrBot（版本 4.29.x）
git clone --depth 1 https://github.com/AstrBotDevs/AstrBot .demo/astrbot
python -m venv .demo/astrbot/venv
.demo/astrbot/venv/Scripts/pip install -r .demo/astrbot/requirements.txt

# 2. 把插件拷进演示实例
Copy-Item plugins/* .demo/astrbot/data/plugins/ -Recurse

# 3. 灌演示数据 → 重置密码 → 起服务
python tools/seed_demo.py
python tools/seed_demo_conv.py
python tools/demo_password.py

# 4. 截图（需要本机有 Edge）
python tools/shot_click.py
```

> 演示数据里的人物、对话、记忆条目全部是编造的（主角「阿岚」，助手「小满」），
> 与任何真实用户无关。这也是 README 里那两张截图的来源。
