#!/usr/bin/env python3
"""Login to v2rayA and start the proxy."""
import json
import socket
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:2017"

def api(path, method="GET", data=None, token=None):
    url = f"{BASE}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()[:500]
            print(f"{method} {path}: {resp.status} | {text[:200]}")
            return resp.status, text
    except urllib.error.HTTPError as e:
        text = e.read().decode()[:500]
        print(f"{method} {path}: HTTP {e.code} | {text[:200]}")
        return e.code, text
    except Exception as e:
        print(f"{method} {path}: ERR {type(e).__name__} {e}")
        return 0, ""

def check_port(port=20170):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", port))
        print(f"PORT {port}: LISTENING")
        return True
    except Exception as e:
        print(f"PORT {port}: {type(e).__name__}")
        return False
    finally:
        s.close()

if __name__ == "__main__":
    # login
    code, text = api("/api/login", "POST", {"username": "ikun", "password": "114514As"})
    token = None
    if code == 200:
        try:
            token = json.loads(text)["data"]["token"]
            print(f"  token: {token[:40]}...")
        except Exception:
            pass

    if token:
        print("\n== current state ==")
        api("/api/touch", "GET", token=token)

        print("\n== trying start endpoints ==")
        for method in ["POST", "PUT", "GET"]:
            for path in ["/api/touch", "/api/start", "/api/running", "/api/v2ray/start", "/api/v2ray/running", "/api/v2ray", "/api/subscribe", "/api/setting"]:
                api(path, method, token=token)

        print("\n== check state again ==")
        api("/api/touch", "GET", token=token)

    print("\n== check port ==")
    check_port(20170)
