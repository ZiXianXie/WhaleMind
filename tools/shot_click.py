"""点击式截图：登录 → 进面板 → 点侧栏「插件」→ 截一张。"""

import json
import subprocess
import time
import urllib.request
from pathlib import Path

import websocket

BASE = "http://127.0.0.1:6195"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = r"E:\temp\edge-cdp-demo"
OUT = Path(__file__).resolve().parents[1] / "docs" / "images"
PORT = 9334


def login():
    body = json.dumps({"username": "demo", "password": "Demo123456"}).encode()
    req = urllib.request.Request(BASE + "/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    return (data.get("data") or {}).get("token") or data.get("token")


class CDP:
    def __init__(self):
        Path(PROFILE).mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen([
            EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=1440,900", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as r:
                    for t in json.loads(r.read().decode()):
                        if t.get("type") == "page":
                            ws_url = t["webSocketDebuggerUrl"]
                            break
            except Exception:
                pass
            if ws_url:
                break
        if not ws_url:
            raise SystemExit("没连上 CDP")
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.mid = 0
        self.call("Page.enable")
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})

    def call(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                return msg

    def goto(self, url, wait=5):
        self.call("Page.navigate", {"url": url})
        time.sleep(wait)

    def click(self, x, y):
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent",
                      {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})
        time.sleep(1.5)

    def shot(self, name):
        res = self.call("Page.captureScreenshot", {"format": "png"})
        p = OUT / name
        import base64
        p.write_bytes(base64.b64decode(res["result"]["data"]))
        print(f"  → {name} ({p.stat().st_size} 字节)")

    def close(self):
        try:
            self.ws.close()
        finally:
            self.proc.kill()


if __name__ == "__main__":
    tk = login()
    print("token ok")
    c = CDP()
    c.goto(BASE + "/", 4)
    c.call("Runtime.evaluate", {"expression":
        f"localStorage.setItem('token', {json.dumps(tk)});"
        "localStorage.setItem('user','demo');"
        "localStorage.setItem('astrbot-locale','zh-CN');"})
    c.goto(BASE + "/", 6)
    c.click(41, 206)          # 侧栏「插件」
    c.shot("02-plugins.png")
    c.close()
