# HYSG 游戏服务端镜像（仅后端；客户端与重打包工具在宿主机使用）
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 时区数据：每日签到/活动重置依赖本地时间
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 依赖版本与开发机（workbuddy Python 3.13 环境）对齐
COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server/sghy_server ./sghy_server
# 空库首次启动时的迁移存档源（SGHY_PLAYER_DATA）
COPY env/player_data.json /seed/player_data.json
COPY server/docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 2724 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import socket; s=socket.create_connection(('127.0.0.1', 2724), 3); s.close()"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "sghy_server"]
