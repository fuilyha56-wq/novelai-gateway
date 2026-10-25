"""Configure a single-attempt policy for NAI image requests in NewAPI.

Default mode previews the rule. --apply persists only the image rule and saves
the previous policy for rollback. Credentials stay in memory and are never printed.
"""
import argparse
import ast
import http.client
import json
from pathlib import Path
import shlex
import sys

import paramiko

ROOT = Path(__file__).resolve().parents[1]
RULE = {
    "name": "nai-image-single-attempt",
    "model_regex": ["^nai-"],
    "path_regex": ["^/v1/images/(generations|edits|img2img|inpainting|vibe-transfer|upscale)(/|$)"],
    "key_sources": [{"type": "context_int", "key": "id"}],
    "value_regex": "",
    "ttl_seconds": 60,
    "skip_retry_on_failure": True,
    "session_mode": "strict",
    "include_using_group": True,
    "include_model_name": True,
    "include_rule_name": True,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--ssh-profile", type=Path, default=ROOT / ".rsh2.py")
    parser.add_argument("--host", help="Override the private SSH profile host")
    args = parser.parse_args()
    if not args.apply and not args.restore:
        print(json.dumps(RULE, ensure_ascii=False, indent=2))
        return
    # Read credential literals without executing a legacy operational script.
    values = {}
    for statement in ast.parse(args.ssh_profile.read_text(encoding="utf-8-sig")).body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in {"HOST", "PORT", "USER", "PASS"}:
                    values[target.id] = ast.literal_eval(statement.value)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(args.host or values["HOST"], port=int(values.get("PORT", 22)), username=values["USER"],
                password=values["PASS"], timeout=20, allow_agent=False, look_for_keys=False)
    try:
        query = ("SELECT row_to_json(t) FROM (SELECT id,access_token FROM users "
                 "WHERE role=100 AND status=1 AND access_token IS NOT NULL "
                 "AND access_token<>'' ORDER BY id LIMIT 1) t")
        _, stdout, stderr = ssh.exec_command(
            "docker exec postgres psql -U root -d new-api -At -c " + shlex.quote(query), timeout=30,
        )
        raw = stdout.read().decode().strip()
        if not raw:
            raise RuntimeError("No existing administrator access token is available")
        user = json.loads(raw)

        def api(method, body=None):
            channel = ssh.get_transport().open_channel("direct-tcpip", ("127.0.0.1", 3000), ("127.0.0.1", 0))
            connection = http.client.HTTPConnection("127.0.0.1", 3000, timeout=20)
            connection.sock = channel
            try:
                connection.request(method, "/api/option/request_policy",
                    body=json.dumps(body) if body is not None else None,
                    headers={"Authorization": "Bearer " + user["access_token"],
                             "New-Api-User": str(user["id"]), "Content-Type": "application/json"})
                response = connection.getresponse()
                result = json.loads(response.read())
                if response.status != 200 or not result.get("success"):
                    raise RuntimeError(f"Policy API rejected the update: {result.get('message', response.status)}")
                return result["data"]["options"]
            finally:
                connection.close()

        current = api("GET")
        if args.restore:
            target_rules = json.loads(args.restore.read_text(encoding="utf-8"))["channel_affinity_setting.rules"]
        else:
            if current.get("channel_affinity_setting.enabled") != "true":
                raise RuntimeError("Image policy requires the existing session-rule engine to be enabled")
            backup = ROOT / ".diagnostics" / "20261014" / "newapi-policy-pre-apply.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                backup.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            rules = json.loads(current["channel_affinity_setting.rules"])
            rules = [RULE] + [r for r in rules if r.get("name") != RULE["name"]]
            target_rules = json.dumps(rules, ensure_ascii=False, separators=(",", ":"))
        updated = api("PATCH", {"options": {"channel_affinity_setting.rules": target_rules}})
        if updated["channel_affinity_setting.rules"] != target_rules:
            raise RuntimeError("Updated policy did not match the requested rules")
        print("Image retry policy updated; global RetryTimes=" + updated["RetryTimes"])
        print("Rules: " + ", ".join(r["name"] for r in json.loads(target_rules)))
    finally:
        ssh.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
