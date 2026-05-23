#!/usr/bin/env bash
# 단일 파일 hotpatch — 컨테이너 내부 gunicorn master 에 SIGHUP 전송하여
# 워커 재시작 (graceful reload). docker compose up -d 보다 빠르게 단일 파일
# 변경만 반영할 때 사용.
#
# 주의: PID 1 은 `uv run` 래퍼이므로 `kill -HUP 1` 은 gunicorn 에 전달되지
# 않는다. /proc 스캔으로 실제 gunicorn master PID 를 찾아 HUP 을 보낸다.
set -euo pipefail

CONTAINER="${CONTAINER:-samba-samba-api-1}"
SRC_FILE="${1:-}"
DST_PATH="${2:-}"

if [[ -z "$SRC_FILE" || -z "$DST_PATH" ]]; then
  echo "사용법: $0 <로컬파일경로> <컨테이너내목적지경로>" >&2
  echo "예) $0 backend/backend/api/v1/routers/samba/proxy/smartstore.py /app/backend/backend/api/v1/routers/samba/proxy/smartstore.py" >&2
  exit 1
fi

if [[ ! -f "$SRC_FILE" ]]; then
  echo "로컬 파일 없음: $SRC_FILE" >&2
  exit 1
fi

echo "[1/3] 컨테이너 $CONTAINER 로 파일 복사 → $DST_PATH"
sudo docker cp "$SRC_FILE" "$CONTAINER:$DST_PATH"

echo "[2/3] gunicorn master PID 탐지"
GUNICORN_PID=$(sudo docker exec "$CONTAINER" sh -c '
  for pid in $(ls /proc 2>/dev/null | grep -E "^[0-9]+$"); do
    cmdline=$(cat /proc/$pid/cmdline 2>/dev/null | tr "\0" " ")
    if echo "$cmdline" | grep -q "gunicorn" \
        && ! echo "$cmdline" | grep -q "uv run" \
        && ! echo "$cmdline" | grep -q "\-\-worker"; then
      # master 는 cmdline 에 worker 옵션이 없거나 부모가 1
      ppid=$(awk "/^PPid:/{print \$2}" /proc/$pid/status 2>/dev/null)
      if [ "$ppid" = "1" ] || [ "$ppid" = "0" ]; then
        echo $pid
        break
      fi
    fi
  done
')

if [[ -z "$GUNICORN_PID" ]]; then
  echo "gunicorn master PID 탐지 실패 — fallback: 모든 gunicorn 프로세스 중 PPid=1 검색" >&2
  GUNICORN_PID=$(sudo docker exec "$CONTAINER" sh -c '
    for pid in $(pgrep -f gunicorn 2>/dev/null); do
      ppid=$(awk "/^PPid:/{print \$2}" /proc/$pid/status 2>/dev/null)
      [ "$ppid" = "1" ] && echo $pid && break
    done
  ')
fi

if [[ -z "$GUNICORN_PID" ]]; then
  echo "ERROR: gunicorn master 를 찾지 못함. docker exec $CONTAINER ps auxf 로 확인" >&2
  exit 2
fi

echo "  gunicorn master PID = $GUNICORN_PID"

echo "[3/3] SIGHUP 전송 — graceful reload"
sudo docker exec "$CONTAINER" kill -HUP "$GUNICORN_PID"

echo "완료. 5초 후 헬스체크:"
sleep 5
sudo docker exec "$CONTAINER" curl -sf http://127.0.0.1:8080/healthz \
  && echo "  헬스체크 OK" \
  || echo "  헬스체크 실패 — 로그 확인 필요: sudo docker logs --tail 100 $CONTAINER"
