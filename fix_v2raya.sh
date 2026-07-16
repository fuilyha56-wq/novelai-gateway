#!/bin/bash
echo "---BEFORE---"
sqlite3 /etc/v2raya/v2raya.db "SELECT key, value FROM system_config WHERE key='system:running';"
echo "---UPDATE---"
sqlite3 /etc/v2raya/v2raya.db "UPDATE system_config SET value='true' WHERE key='system:running';"
echo "UPDATED"
echo "---AFTER---"
sqlite3 /etc/v2raya/v2raya.db "SELECT key, value FROM system_config WHERE key='system:running';"
echo "---RESTART---"
docker restart v2raya
sleep 8
echo "---CHECK_PORT---"
ss -tlnp 2>/dev/null | grep 20170 || echo "NOT_LISTENING_20170"
echo "---V2RAYA_LOGS---"
docker logs --tail 15 v2raya 2>&1 | tail -n 15
