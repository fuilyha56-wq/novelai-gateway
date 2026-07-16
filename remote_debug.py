import os
import subprocess

commands = [
    'echo ===== pwd =====',
    'pwd',
    'echo ===== docker ps =====',
    'docker ps --filter name=novelai-gateway --format "{{.ID}} {{.Names}} {{.Status}}"',
    'echo ===== docker inspect =====',
    'docker inspect novelai-gateway | tail -n 20',
    'echo ===== docker logs last 50 =====',
    'docker logs --tail 50 novelai-gateway',
    'echo ===== env file =====',
    'cat /app/.env || true',
    'echo ===== env vars =====',
    'docker exec novelai-gateway env | grep -E "SHARED_API_KEY|SHARED_TOKEN|HOST|PORT|http_proxy|https_proxy|all_proxy" || true',
    'echo ===== netstat =====',
    'docker exec novelai-gateway netstat -anp 2>/dev/null | grep 41555 || true',
]
for cmd in commands:
    print(cmd)
    try:
        output = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, text=True)
        print(output)
    except subprocess.CalledProcessError as exc:
        print(exc.output)
        print(f"EXIT:{exc.returncode}")
