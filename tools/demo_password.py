
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASTRBOT_HOME = Path(os.environ.get("ASTRBOT_HOME", str(REPO / ".demo" / "astrbot")))
"""把演示实例的面板密码重设为 demo / Demo123456（只动 E:\\AstrBot-demo）。"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(ASTRBOT_HOME))
from astrbot.core.utils.auth_password import (  # noqa: E402
    hash_dashboard_password,
    hash_md5_dashboard_password,
)

CFG = ASTRBOT_HOME / "data" / "cmd_config.json"
USER, PWD = "demo", "Demo123456"

cfg = json.loads(CFG.read_text(encoding="utf-8-sig"))
dash = cfg["dashboard"]
dash["username"] = USER
dash["password"] = hash_md5_dashboard_password(PWD)
dash["pbkdf2_password"] = hash_dashboard_password(PWD)
dash["password_change_required"] = False
CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已重置：{USER} / {PWD}，端口 {dash['port']}，host {dash['host']}")
print("md5:", hashlib.md5(PWD.encode()).hexdigest()[:12], "…")
