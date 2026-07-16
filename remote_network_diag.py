#!/usr/bin/env python3
"""Remote host network diagnostic script for NovelAI connectivity."""
import socket
import ssl
import subprocess
from typing import Iterable

TARGETS = [
    ("www.google.com", 443),
    ("image.novelai.net", 443),
    ("example.com", 443),
    ("www.cloudflare.com", 443),
]
IP_TESTS = [
    ("google-ip", "31.13.92.37", 443),
    ("novelai-ip", "199.59.148.89", 443),
    ("example-http-ip", "104.20.23.154", 80),
]


def run_cmd(cmd: list[str]) -> None:
    print("$", " ".join(cmd))
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        print("ERR", type(exc).__name__, exc)
        return
    print("RETURN", completed.returncode)
    if completed.stdout:
        print(completed.stdout.strip())
    if completed.stderr:
        print(completed.stderr.strip())


def test_socket(host: str, port: int, timeout: float = 8.0) -> None:
    print(f"socket {host}:{port}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        print("  connected")
    except Exception as exc:
        print("  error", type(exc).__name__, exc)
    finally:
        sock.close()


def test_tls(host: str, port: int = 443, timeout: float = 8.0) -> None:
    print(f"tls {host}:{port}")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host):
                print("  tls ok")
    except Exception as exc:
        print("  tls error", type(exc).__name__, exc)


def test_http(url: str, timeout: float = 18.0) -> None:
    print(f"http {url}")
    run_cmd(["curl", "-v", "--ipv4", "--connect-timeout", str(int(timeout / 2)), "--max-time", str(int(timeout)), url])


def main() -> None:
    print("== host info ==")
    run_cmd(["uname", "-a"])
    run_cmd(["ip", "addr", "show"])
    run_cmd(["ip", "route", "show"])
    run_cmd(["cat", "/etc/resolv.conf"])

    print("== dns ==")
    for host, _ in TARGETS:
        run_cmd(["getent", "hosts", host])
        run_cmd(["dig", "+short", host])

    print("== curl tests ==")
    for host, _ in TARGETS:
        test_http(f"https://{host}")

    print("== socket tests ==")
    for name, ip, port in IP_TESTS:
        test_socket(ip, port)
        if port == 443:
            test_tls(ip, port)

    print("== route get ==")
    for _, ip, _ in IP_TESTS:
        run_cmd(["ip", "route", "get", ip])


if __name__ == "__main__":
    main()
