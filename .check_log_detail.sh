#!/bin/sh
docker exec postgres psql -U root -d new-api -x -c "SELECT * FROM logs WHERE model_name LIKE 'nai-%' ORDER BY id DESC LIMIT 1;"
