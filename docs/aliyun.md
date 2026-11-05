# 公网服务器部署（后端）

目标：服务端跑在公网服务器 `43.136.135.75`，APK 在任何网络都能连接。

```
客户端 APK ──HTTP :8000──> serverlist（返回 43.136.135.75:2724）
           ──TCP  :2724──> 游戏服务
```

## 1. 安全组与网络

云服务器控制台 → 安全组，入方向放行：

| 协议 | 端口 | 来源 | 用途 |
|---|---|---|---|
| TCP | 2724 | 0.0.0.0/0 | 游戏协议 |
| TCP | 8000 | 0.0.0.0/0 | 服务器列表 / 公告 / 邮件页 / GM |
| TCP | 22 | 你的办公 IP | SSH（建议收窄） |

系统防火墙（Alibaba Cloud Linux / CentOS）：

```bash
sudo firewall-cmd --permanent --add-port=2724/tcp --add-port=8000/tcp
sudo firewall-cmd --reload
# Ubuntu: sudo ufw allow 2724/tcp && sudo ufw allow 8000/tcp
```

建议绑定**弹性公网 IP（EIP）**：APK 里写死了 HTTP 地址，公网 IP 变化就要重新打包。

## 2. 安装 Docker

```bash
# Alibaba Cloud Linux 3 / CentOS
sudo dnf install -y docker
sudo systemctl enable --now docker

# 安装 compose 插件（任选其一）
sudo dnf install -y docker-compose-plugin || {
  sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
    -o /usr/local/bin/docker-compose
  sudo chmod +x /usr/local/bin/docker-compose
}
docker --version && docker compose version || docker-compose version
```

> 国内拉镜像慢时可给 Docker 配镜像加速（阿里云容器镜像服务控制台有专属地址），
> 或在本地 `docker build` 后推送到 ACR 再 `docker pull`。

## 3. 上传代码

```bash
# 方式一：git（仓库包含客户端资源，首次约 300MB+）
git clone https://gitee.com/t1longlive/hysg.git HYSG

# 方式二：本地打包上传（排除 build/config_src 等）
#   scp -r HYSG root@43.136.135.75:/opt/
```

只需要后端时，至少要有：`server/`、`env/_work/rt_all/`、`env/player_data.json`、
`docker-compose.yml`、`.dockerignore`、`server/Dockerfile`。

## 4. 迁移存档

`server/data/` 不在 git 里，需要单独上传（当前存档在开发机）：

```bash
# 开发机（Windows，Git Bash）
scp server/data/sghy.db server/data/sghy.db-wal server/data/sghy.db-shm \
    root@43.136.135.75:/opt/HYSG/server/data/
```

容器**首次启动**会把 `server/data/sghy.db*` 只读挂载到 `/seed-db` 导入到卷
`hysg-server-data`；之后只看卷，不再读 `server/data`。

## 5. 配置环境变量

在 ECS 的仓库根创建 `.env`：

```bash
cd /opt/HYSG
cat > .env <<'EOF'
SGHY_PUBLIC_IP=43.136.135.75
SGHY_SERVER_NAME=三国的后裔
SGHY_GM_TOKEN=换成一段随机字符串
EOF
chmod 600 .env
```

- `SGHY_PUBLIC_IP`：serverlist 下发给客户端的地址，必须是 **43.136.135.75**
- `SGHY_GM_TOKEN`：**公网必须设置**，否则任何人可访问 `/api/gm` 和 `/mail` 发道具
- 生成随机令牌：`openssl rand -hex 16`

## 6. 启动

```bash
cd /opt/HYSG
docker compose up -d --build
docker compose ps          # STATUS 应为 healthy
docker compose logs -f server
```

验证（本机与外部都要通）：

```bash
curl http://127.0.0.1:8000/api/getserverlist
curl http://43.136.135.75:8000/api/getserverlist   # 返回 ip1=43.136.135.75, game_port=2724
```

## 7. 重新打包 APK

在开发机上（HTTP 地址烧进 APK）：

```bash
python tools/android_repack.py --server-host 43.136.135.75:8000
# 装到本机调试：加 --install --launch
```

客户端流程：先请求 `http://43.136.135.75:8000/api/getserverlist`，再按返回的
`43.136.135.75:2724` 连游戏服。换公网 IP 后需要重新打包。

## 8. GM / 发信台

```bash
# 查询接口（token 也可放请求头 X-GM-Token）
curl "http://43.136.135.75:8000/api/gm?action=reload_config&token=<TOKEN>"
```

浏览器打开发信台：`http://43.136.135.75:8000/mail?token=<TOKEN>`
（页面会把 token 自动带到后续 `/api/mail/*` 请求）

未带 token 时这些接口返回 403；`SGHY_GM_TOKEN` 为空则保持旧的免鉴权行为（仅本地用）。

## 9. 日常运维

```bash
docker compose logs -f server        # 日志
docker compose restart server        # 重启（配置/存档都在卷里）
docker compose down                  # 停止
docker compose up -d --build         # 更新代码后重建

# 存档备份（先停，保证 WAL 一致）
docker compose stop
docker run --rm -v hysg-server-data:/data -v "$PWD:/backup" \
  alpine sh -c "tar czf /backup/sghy-data-$(date +%Y%m%d).tgz -C /data ."
docker compose start
```

- 服务器时区：镜像与 compose 已设 `TZ=Asia/Shanghai`，签到/活动按北京时间重置
- 配置更新：开发机改 Excel → `config_tool.py build --no-reload` → 把
  `env/_work/rt_all` 同步到 ECS（容器只读挂载）→ 调 reload 接口；玩家重新登录生效
- 单机 SQLite 适合小规模；玩家多再换 MySQL/PostgreSQL

## 10. 排错

| 现象 | 处理 |
|---|---|
| 外部访问 8000 超时 | 安全组/防火墙未放行；确认容器端口映射 |
| 能登录但进游戏卡住 | `SGHY_PUBLIC_IP` 没改成公网 IP，客户端在连 127.0.0.1 |
| APK 仍连旧地址 | APK 里的 HTTP 地址是打包时烧入的，必须重新打包 |
| `disk I/O error` | 存档放了绑定挂载；用本仓库的原生卷方案 |
| 403 forbidden | GM/mail 请求没带 `token`（compose 设置了 `SGHY_GM_TOKEN`） |
| 时间活动提前/延后 | 检查 `docker compose exec server date` 是否为 CST |
