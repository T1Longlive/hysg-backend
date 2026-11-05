#!/usr/bin/env bash
# ======================================================================
# HYSG 服务端部署脚本（GitHub Actions 通过 SSH 调用；也可在服务器手工执行）
#
# 用法（在服务器 /opt/HYSG 下）：
#   IMAGE_TAG=<commit-sha> /bin/bash scripts/deploy.sh
#
# 约定：
#   · 镜像由 GitHub Actions 构建并推送到阿里云 ACR，服务器只拉取，不本地构建
#   · 配置文件走绑定挂载（env/_work/rt_all、server/sghy_server/config），
#     不随镜像发布；改配置后重载或重启容器即可
#   · flock 防止两次部署并发把容器状态搞乱
# ======================================================================
set -Eeuo pipefail

APP_DIR="/opt/HYSG"
LOCK_FILE="/tmp/hysg-server-deploy.lock"
LOG_FILE="/var/log/hysg-server-deploy.log"
CONTAINER="hysg-server"
IMAGE_TAG="${IMAGE_TAG:?需要 IMAGE_TAG（CI 传入的 commit sha）}"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"; }

# ---------- 并发保护 ----------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Another deployment is already running, exiting"
  exit 1
fi

cd "$APP_DIR"
export IMAGE_TAG

log "开始部署: IMAGE_TAG=$IMAGE_TAG"

# ---------- 拉取镜像 ----------
log "docker compose pull $CONTAINER"
docker compose pull server

# ---------- 重建容器（禁止本地 build）----------
log "docker compose up -d --no-build"
docker compose up -d --no-build server

# ---------- 健康检查 ----------
STATUS=""
for i in $(seq 1 18); do
  STATUS=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER" 2>/dev/null || true)
  log "健康检查 [$i/18] $STATUS"
  if [ "$STATUS" = "healthy" ]; then
    break
  fi
  sleep 5
done

if [ "$STATUS" != "healthy" ]; then
  log "部署失败：容器状态=$STATUS，最近日志如下"
  docker logs --tail 40 "$CONTAINER" 2>&1 | tee -a "$LOG_FILE"
  exit 1
fi

log "部署完成: IMAGE_TAG=$IMAGE_TAG"
