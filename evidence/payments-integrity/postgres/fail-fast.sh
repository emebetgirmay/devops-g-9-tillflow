#!/usr/bin/env bash
# usage: failfast.sh IMAGE LABEL -> JSON line with timings
set -u
IMG=$1; LABEL=$2
docker rm -f g9-pg g9-pay >/dev/null 2>&1
docker run -d --name g9-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=tillflow -p 127.0.0.1:55440:5432 postgres:16 >/dev/null
for i in $(seq 60); do [ "$(docker logs g9-pg 2>&1 | grep -c 'ready to accept connections')" -ge 2 ] && break; sleep 1; done
docker exec g9-pg psql -U postgres -d tillflow -qc "create schema payments" >/dev/null
docker run -d --name g9-pay --network host -e PORT=8080 -e DATABASE_URL='postgresql://postgres:test@127.0.0.1:55440/tillflow?options=-csearch_path%3Dpayments' "$IMG" >/dev/null
for i in $(seq 60); do curl -sf --max-time 2 localhost:8080/ready >/dev/null && break; sleep 0.5; done
docker stop -t 1 g9-pg >/dev/null
DOWN=$(date +%s.%N)
worst=0; codes=""
for i in 1 2 3; do
  r=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 45 localhost:8080/ready); codes="$codes ${r%% *}"
  t=${r##* }; worst=$(python3 -c "print(max($worst,$t))")
done
docker start g9-pg >/dev/null
UP=$(date +%s.%N)
for i in $(seq 200); do [ "$(docker logs g9-pg 2>&1 | grep -c 'ready to accept connections')" -ge 3 ] && break; sleep 0.2; done
DBREADY=$(date +%s.%N)
for i in $(seq 600); do curl -sf --max-time 45 localhost:8080/ready >/dev/null && break; sleep 0.1; done
SVC=$(date +%s.%N)
python3 -c "import json; print(json.dumps({'image': '$LABEL', 'while_db_down_ready_codes': '$codes'.split(), 'slowest_request_while_down_s': round($worst,2), 'payments_ready_after_db_s': round($SVC-$DBREADY,2)}))"
docker rm -f g9-pg g9-pay >/dev/null 2>&1
