#!/bin/sh
docker exec postgres psql -U root -d new-api -c "SELECT model_name, quota, prompt_tokens FROM logs WHERE model_name LIKE 'nai-%' ORDER BY id DESC LIMIT 5;"
