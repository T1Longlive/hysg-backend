#!/bin/sh
# 首次启动：把宿主机现有存档（/seed-db，只读挂载）导入 Docker 原生卷 /data。
# SQLite WAL 在 Windows 绑定挂载上会报 disk I/O error，所以运行期必须用卷。
set -e

mkdir -p /data /logs
if [ ! -f /data/sghy.db ] && [ -f /seed-db/sghy.db ]; then
    echo "[entrypoint] 首次启动：从 /seed-db 导入现有存档"
    for f in sghy.db sghy.db-wal sghy.db-shm; do
        [ -f "/seed-db/$f" ] && cp "/seed-db/$f" /data/ || true
    done
fi

exec "$@"
