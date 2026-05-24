#!/bin/bash
# 로컬 수동 배포 스크립트 — Docker build → AR push → VM deploy → 카카오 알림
#
# 사전 준비:
# 1. Docker Desktop 실행 중
# 2. gcloud auth configure-docker asia-northeast3-docker.pkg.dev (최초 1회)
# 3. ~/samba-vm-secrets/deploy.env 파일 작성 (카카오 토큰)
# 4. ~/samba-vm-secrets/ssh/deploy_key 존재 확인
#
# 실행:
#   bash scripts/deploy.sh              # 일반 배포
#   bash scripts/deploy.sh --skip-kakao # 카카오 알림 건너뛰기
#
# 소요 시간: 약 3~5분 (캐시 적중 시 1~2분)

set -e

# ─────────────────────────────────────
# 설정
# ─────────────────────────────────────
PROJECT_ID="fresh-sanctuary-489804-v4"
AR_REGION="asia-northeast3"
AR_REPO="cloud-run-source-deploy"
IMAGE_NAME="samba-wave-api"
VM_HOST="api.samba-wave.co.kr"
VM_USER="sbk0674"
SSH_KEY="$HOME/samba-vm-secrets/ssh/deploy_key"
ENV_FILE="$HOME/samba-vm-secrets/deploy.env"

IMAGE="${AR_REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}"
SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "local")
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
START_TIME=$(date +%s)

# 옵션 파싱
SKIP_KAKAO=false
NO_CACHE=false
for arg in "$@"; do
  case "$arg" in
    --skip-kakao) SKIP_KAKAO=true ;;
    --no-cache)   NO_CACHE=true ;;
  esac
done

if [[ -f "$ENV_FILE" ]] && [[ "$SKIP_KAKAO" == "false" ]]; then
  set +e
  source "$ENV_FILE"
  set -e
fi

# ─────────────────────────────────────
# 컬러 출력
# ─────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 단계별 소요시간 추적 — log_step 호출 시점에 STAGE_START 갱신, log_ok에서 delta 표기
STAGE_START=$(date +%s)
log_step() {
  STAGE_START=$(date +%s)
  local total_elapsed=$((STAGE_START - START_TIME))
  echo -e "${BLUE}[$(date +%H:%M:%S)] [+${total_elapsed}s] [$1/$2]${NC} $3"
}
log_ok() {
  local now=$(date +%s)
  local stage_elapsed=$((now - STAGE_START))
  local total_elapsed=$((now - START_TIME))
  echo -e "${GREEN}✅ $1 (단계 ${stage_elapsed}s, 누적 ${total_elapsed}s)${NC}"
}
log_err()  { echo -e "${RED}❌ $1${NC}"; }

# ─────────────────────────────────────
# 카카오 알림 함수
# ─────────────────────────────────────
# Git Bash(Windows) CP949 이슈로 쉘에서 한글 전달이 깨져서 오므로
# 한글 라벨은 deploy/kakao_notify.py 내부에 두고, 쉘에서는 ASCII 인자만 전달한다.
kakao_notify() {
  local status="$1"            # success | fail
  local failure_reason="${2:-}"  # healthcheck | build | push | ssh | generic | ""
  local exit_code="${3:-0}"
  local elapsed="${4:-0}"

  if [[ "$SKIP_KAKAO" == "true" ]] || [[ -z "${KAKAO_API_KEY:-}" ]] || [[ -z "${KAKAO_REFRESH_TOKEN:-}" ]]; then
    return 0
  fi

  python "$(dirname "${BASH_SOURCE[0]}")/kakao_notify.py" \
    --status "$status" \
    --sha "$SHA" \
    --branch "$BRANCH" \
    --failure-reason "$failure_reason" \
    --exit-code "$exit_code" \
    --elapsed "$elapsed" \
    --api-key "$KAKAO_API_KEY" \
    --refresh-token "$KAKAO_REFRESH_TOKEN" \
    > /dev/null 2>&1 || true
}

# ─────────────────────────────────────
# 에러 핸들러 — 어느 단계라도 실패하면 알림
# ─────────────────────────────────────
trap 'on_error $?' ERR
on_error() {
  local exit_code=$1
  local elapsed=$(($(date +%s) - START_TIME))
  log_err "배포 실패 (exit $exit_code, ${elapsed}초)"
  kakao_notify "fail" "generic" "$exit_code" "$elapsed"
  exit "$exit_code"
}

# ─────────────────────────────────────
# 배포 시작
# ─────────────────────────────────────
echo -e "${YELLOW}🚀 Samba Wave 배포 시작${NC}"
echo "   커밋: $SHA ($BRANCH)"
echo "   이미지: $IMAGE"
echo ""

# 1. Docker 빌드 (캐시 활용, --no-cache 옵션 지원)
log_step 1 4 "Docker 이미지 빌드 중..."
DEPLOYED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
# 확장앱 자가업뎃 단일 출처 — manifest.json version 을 백엔드에 주입.
# (스크립트 위치 기준 경로 → 어디서 실행해도 동일)
_DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_VERSION=$(jq -r '.version' "$_DEPLOY_DIR/../extension/manifest.json" 2>/dev/null || echo "")
BUILD_ARGS=(
  --platform linux/amd64
  --build-arg BUILDKIT_INLINE_CACHE=1
  --build-arg "COMMIT_SHA=$SHA"
  --build-arg "DEPLOYED_AT=$DEPLOYED_AT"
  --build-arg "EXT_VERSION=$EXT_VERSION"
)
if [[ "$NO_CACHE" == "true" ]]; then
  echo "   ⚠️ --no-cache 모드: 전체 재빌드 (5~10분 소요)"
  BUILD_ARGS+=(--no-cache)
else
  BUILD_ARGS+=(--cache-from "$IMAGE:latest")
fi
DOCKER_BUILDKIT=1 docker build \
  "${BUILD_ARGS[@]}" \
  -t "$IMAGE:$SHA" \
  -t "$IMAGE:latest" \
  ./backend
log_ok "빌드 완료"

# 2. AR 푸시
log_step 2 4 "Artifact Registry 푸시 중..."
docker push "$IMAGE:$SHA"
docker push "$IMAGE:latest"
log_ok "푸시 완료"

# 2-1. 로컬 Docker 이미지 정리 — latest + 현재 SHA 외 이전 빌드 태그 제거 (디스크 누적 방지)
# Build Cache는 보존 (다음 build 속도 — BUILDKIT_INLINE_CACHE 활용)
docker images --filter reference="$IMAGE" --format "{{.ID}} {{.Tag}}" \
    | grep -vE "latest|$SHA" | awk '{print $1}' | sort -u \
    | xargs -r docker rmi -f > /dev/null 2>&1 || true

# 3. VM 배포 — Blue/Green 무중단
#    흐름: green 띄우기 → green 헬스OK → blue stop(Caddy 자동 fallback) → blue 새 이미지로 재시작
#         → blue 헬스OK(Caddy first 우선이라 자동 복귀) → green stop → 미사용 이미지 정리

# 3-0. VM 설정 파일 동기화 (docker-compose.yml + Caddyfile)
log_step 3 4 "VM 설정 파일 동기화 중..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new \
    "$SCRIPT_DIR/vm/docker-compose.yml" \
    "$SCRIPT_DIR/vm/Caddyfile" \
    "${VM_USER}@${VM_HOST}:/tmp/" > /dev/null
ssh -i "$SSH_KEY" "${VM_USER}@${VM_HOST}" 'bash -s' << 'INIT_SCRIPT'
# BG_WORKER_TOKEN 자동 생성 (최초 1회)
if ! grep -q "BG_WORKER_TOKEN" /opt/samba/.env 2>/dev/null; then
    TOKEN=$(python3 -c "import uuid; print(uuid.uuid4())")
    echo "BG_WORKER_TOKEN=${TOKEN}" >> /opt/samba/.env
    echo "    BG_WORKER_TOKEN 자동 생성 완료"
fi
INIT_SCRIPT
ssh -i "$SSH_KEY" "${VM_USER}@${VM_HOST}" '
    set -e
    if ! sudo cmp -s /tmp/docker-compose.yml /opt/samba/docker-compose.yml; then
        sudo cp /tmp/docker-compose.yml /opt/samba/docker-compose.yml
        sudo chown ubuntu:ubuntu /opt/samba/docker-compose.yml
        echo "    docker-compose.yml 갱신됨"
    fi
    CADDY_CHANGED=false
    if ! sudo cmp -s /tmp/Caddyfile /opt/samba/Caddyfile; then
        sudo cp /tmp/Caddyfile /opt/samba/Caddyfile
        sudo chown ubuntu:ubuntu /opt/samba/Caddyfile
        CADDY_CHANGED=true
        echo "    Caddyfile 갱신됨 — reload 예정"
    fi
    if [ "$CADDY_CHANGED" = "true" ]; then
        sudo docker exec samba-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
        echo "    ✅ Caddy reload 완료"
    fi
'
log_ok "설정 파일 동기화 완료"

log_step 3 4 "VM Blue/Green 배포 중 (${VM_HOST})..."
ssh -i "$SSH_KEY" \
  -o StrictHostKeyChecking=accept-new \
  -o ConnectTimeout=10 \
  "${VM_USER}@${VM_HOST}" \
  "bash -s -- $SHA" << 'REMOTE_SCRIPT'
set -e
SHA="$1"  # 로컬 deploy.sh 에서 인자로 전달 (heredoc single-quote 라 변수 확장 안 됨)
cd /opt/samba
REMOTE_START=$(date +%s)
remote_step() {
  local now=$(date +%s)
  local elapsed=$((now - REMOTE_START))
  echo "[$(date +%H:%M:%S)] [+${elapsed}s] $1"
}

remote_step "[1/6] green(staging) 이미지 pull..."
sudo docker compose --profile staging pull samba-api-staging

remote_step "[2/6] green 컨테이너 시작..."
sudo docker compose --profile staging up -d samba-api-staging

remote_step "[3/6] green 헬스체크 대기 (최대 360초)..."
# docker healthcheck status 가 아닌 컨테이너 내부 wget 으로 직접 폴링.
# 이유: docker healthcheck는 interval=30s + start_period=60s 라서 첫 healthy 갱신까지 90~150초 걸림.
# 직접 wget 은 startup 즉시 응답하므로 실제 readiness 와 일치.
# 폴링 간격 5s→2s로 단축 (배포시간 최적화 — startup 50s 가정 시 평균 20~30초 절감)
# 타임아웃 180s→360s (2026-04-28): emergency schema fix가 blue idle connection lock 대기로
#   4분 41초 hang하던 사례 발생. entrypoint.sh에 lock_timeout=5s 보호 적용으로 즉시 fail-fast
#   되긴 하지만, 마이그레이션 누적분 + verify_schema + lifespan startup 합산 안전 마진 위해 360s.
for i in $(seq 1 180); do
    sleep 2
    RESP=$(sudo docker exec samba-samba-api-staging-1 wget -qO- --timeout=3 http://localhost:8080/api/v1/health 2>/dev/null || echo "")
    if echo "$RESP" | grep -q '"status":"healthy"'; then
        echo "    ✅ green healthy (${i}회 시도, $((i*2))초)"
        break
    fi
    echo "    ⏳ green not-ready ($i/180, $((i*2))초)"
    if [ "$i" = "180" ]; then
        echo "❌ green 헬스체크 실패"
        echo ""
        echo "─── staging 컨테이너 상태 ─────────────────────────────"
        sudo docker inspect samba-samba-api-staging-1 \
          --format 'status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} startedAt={{.State.StartedAt}}' 2>&1 || true
        echo ""
        echo "─── staging 컨테이너 로그 (마지막 200줄) ──────────────"
        sudo docker logs samba-samba-api-staging-1 --tail 200 2>&1 || true
        echo "─────────────────────────────────────────────────────"
        echo "정리 후 종료"
        sudo docker compose --profile staging stop samba-api-staging
        sudo docker compose --profile staging rm -f samba-api-staging
        exit 1
    fi
done

remote_step "[4/6] blue drain → stop (Caddy가 503 감지 후 green으로 사전 전환)..."
# graceful drain: blue 가 503 반환하도록 신호 → Caddy active health 가 즉시 fail 감지 →
# 트래픽이 미리 green 으로 전환된 후 stop → 진정한 무중단 (in-flight 요청 5xx 0건)
sudo docker exec samba-samba-api-1 touch /tmp/draining 2>/dev/null || true
sleep 3  # Caddy active health_interval(2s) × 1회 + 여유 = 3초로 단축 (5s→3s)
sudo docker compose stop samba-api
sleep 2

remote_step "[5/6] blue 새 이미지 pull + 재시작..."
sudo docker compose pull samba-api
sudo docker compose up -d samba-api
# /tmp/draining 잔존 시 health 영구 503 → blue 무한 not-ready (2026-04-29 사고).
# 컨테이너 재시작 시 동일 이미지 SHA면 recreate 안 되어 /tmp/draining 살아남음.
# 컨테이너가 PID 1까지 올라올 때까지 잠깐 대기 후 명시적 삭제.
sleep 1
sudo docker exec samba-samba-api-1 rm -f /tmp/draining 2>/dev/null || true
# blue 컨테이너 직접 폴링 (docker exec wget) — 외부 polling은 green이 응답해도 통과되어
# blue ready 판별 불가능했음(둘 다 latest 이미지라 SHA 동일).
# blue startup 5-10분 소요 가능 (alembic + verify_schema + worker 초기화) → 600초 대기.
# 폴링 간격 5s→2s로 단축 (배포시간 최적화 — startup 60s 가정 시 평균 30~50초 절감)
for i in $(seq 1 300); do
    sleep 2
    RESP=$(sudo docker exec samba-samba-api-1 wget -qO- --timeout=3 http://localhost:8080/api/v1/health 2>/dev/null || echo "")
    if echo "$RESP" | grep -q '"status":"healthy"'; then
        echo "    ✅ blue healthy (${i}회 시도, $((i*2))초)"
        break
    fi
    echo "    ⏳ blue not-ready ($i/300, $((i*2))초)"
    if [ "$i" = "300" ]; then
        echo "⚠️ blue 헬스체크 실패 — green 유지 (수동 복구 필요)"
        exit 1
    fi
done

remote_step "[6/6] green 컨테이너 정리 + 미사용 이미지 prune..."
# blue healthy 후 Caddy active health(interval 2s)가 blue로 라우팅 복귀할 시간 확보 —
# active health interval(2s) × 2회 = 4초면 first-priority blue로 복귀 충분 (10s→4s).
sleep 4
sudo docker compose --profile staging stop samba-api-staging
sudo docker compose --profile staging rm -f samba-api-staging
sudo docker image prune -a -f | tail -3

# bg-worker 컨테이너가 남아있다면 정리 (2026-04-29 부터 VM에서 미운영, 로컬PC에서 실행)
if sudo docker ps -a --format '{{.Names}}' | grep -q '^samba-samba-bg-worker-1$'; then
  echo "[+] 잔여 bg-worker 컨테이너 정리..."
  sudo docker stop samba-samba-bg-worker-1 >/dev/null 2>&1 || true
  sudo docker rm -f samba-samba-bg-worker-1 >/dev/null 2>&1 || true
  echo "    ✅ bg-worker 정리 완료"
fi

echo ""
echo "=== 최종 상태 ==="
sudo docker compose ps --format 'table {{.Name}}\t{{.Status}}'
REMOTE_SCRIPT
log_ok "VM Blue/Green 배포 완료"

# 4. 헬스체크 (최대 90초 대기) — 커밋 SHA로 최신 리비전 서빙 중인지 검증
# VM 내부에서 이미 blue healthy 검증했으므로 외부 폴링 간격 10s→3s로 단축
log_step 4 4 "헬스체크 중..."
HEALTH_OK=false
for i in $(seq 1 30); do
  sleep 3
  RESP=$(curl -sS -m 10 "https://${VM_HOST}/api/v1/health" 2>/dev/null || echo "")
  STATUS=$(echo "$RESP" | grep -oE '"status":"[^"]*"' | head -1 | cut -d'"' -f4)
  LIVE_SHA=$(echo "$RESP" | grep -oE '"commit":"[^"]*"' | head -1 | cut -d'"' -f4)
  if [[ "$STATUS" == "healthy" ]]; then
    if [[ "$LIVE_SHA" == "$SHA" ]]; then
      log_ok "HTTP 200 + 커밋 $LIVE_SHA 확인 (최신 리비전 서빙 중, 시도 $i)"
    else
      log_ok "HTTP 200 응답 확인 (라이브 커밋: ${LIVE_SHA:-unknown}, 예상: $SHA, 시도 $i)"
    fi
    HEALTH_OK=true
    break
  fi
  echo "   시도 $i/30: status=${STATUS:-none} commit=${LIVE_SHA:-none} — 재시도"
done
if [[ "$HEALTH_OK" != "true" ]]; then
  ELAPSED=$(($(date +%s) - START_TIME))
  log_err "헬스체크 실패 — 최신 리비전이 서빙되지 않음"
  kakao_notify "fail" "healthcheck" "1" "$ELAPSED"
  exit 1
fi

# 성공
ELAPSED=$(($(date +%s) - START_TIME))
echo ""
echo -e "${GREEN}🎉 배포 완료 (총 ${ELAPSED}초)${NC}"
echo "   https://${VM_HOST}"
kakao_notify "success" "" "0" "$ELAPSED"
