"""用 CDP 驱动无头 Edge，登录演示实例并截几张 README 用的图。

只连 127.0.0.1:6195（演示实例），不碰真实实例。
"""

import base64
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
PORT = 9333


def login():
    body = json.dumps({"username": "demo", "password": "Demo123456"}).encode()
    req = urllib.request.Request(
        BASE + "/api/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    token = (data.get("data") or {}).get("token") or data.get("token")
    if not token:
        raise SystemExit(f"登录失败：{data}")
    print("登录成功，token 前 12 位：", token[:12])
    return token


def cdp_call(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            return msg


def shoot(url, out_name, token, wait=7, full_height=900):
    Path(PROFILE).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
        "--no-first-run", "--no-default-browser-check",
        f"--window-size=1440,{full_height}", "about:blank",
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
        proc.kill()
        raise SystemExit("没连上 CDP")

    ws = websocket.create_connection(ws_url, timeout=30)
    mid = 0
    mid += 1
    cdp_call(ws, mid, "Page.enable")
    mid += 1
    cdp_call(ws, mid, "Emulation.setDeviceMetricsOverride",
             {"width": 1440, "height": full_height, "deviceScaleFactor": 1, "mobile": False})
    mid += 1
    cdp_call(ws, mid, "Page.navigate", {"url": BASE + "/"})
    time.sleep(3)
    mid += 1
    cdp_call(ws, mid, "Runtime.evaluate", {
        "expression": f"localStorage.setItem('token', {json.dumps(token)});"
                      f"localStorage.setItem('user', 'demo');"
                      f"localStorage.setItem('astrbot-locale','zh-CN');"})
    mid += 1
    cdp_call(ws, mid, "Page.navigate", {"url": url})
    time.sleep(wait)
    mid += 1
    res = cdp_call(ws, mid, "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    data = res["result"]["data"]
    target = OUT / out_name
    OUT.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(data))
    print(f"已保存 {target}（{target.stat().st_size} 字节）")
    ws.close()
    proc.kill()
    time.sleep(1)


if __name__ == "__main__":
    tk = login()
    shoot(BASE + "/#/plugins", "02-plugins.png", tk)
    shoot(BASE + "/#/plugins", "03-plugin-config.png", tk, wait=9)
