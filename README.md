# HYSG 服务端（部署仓库）

「三国的后裔」自建服务端的**部署用仓库**：只包含后端代码、Docker 构建与 CI/CD 配置。
客户端资源、编辑用 Excel、完整配置管线仍在 Gitee 主仓库（`t1longlive/hysg`）。

## 目录

```
server/            Python 3.13 服务端（TCP 2724 / HTTP 8000）
  Dockerfile       运行镜像
  sghy_server/     代码与配置（activities.json / level_drop.json）
env/player_data.json  空库首次启动的迁移存档源（镜像内置 /seed/player_data.json）
docker-compose.yml    部署编排（镜像来自阿里云 ACR）
scripts/deploy.sh     服务器端部署脚本（CI 远程调用，也可手工执行）
docs/                 部署/配置相关文档
```

> `env/_work/rt_all/`（约 51MB 配置表）**不在本仓库**，它是运行期只读绑定挂载，
> 由主仓库/服务器维护。部署机 `/opt/HYSG` 下必须有该目录，否则容器配置加载失败。

## CI/CD

```
push main → GitHub Actions
  ├─ build：docker build → 推送 ACR（tag = commit sha）
  └─ deploy：SSH 到服务器 → docker compose pull → up -d --no-build → 健康检查
```

触发路径：`server/**`、`env/player_data.json`、`docker-compose.yml`、`scripts/**`、workflow 本身；
文档改动不会触发部署。也可在 Actions 页面手动 `Run workflow`。

### 仓库 Secrets

| Name | 说明 |
|---|---|
| `ACR_USERNAME` | 阿里云 ACR 用户名（与 pm 项目一致） |
| `ACR_PASSWORD` | 阿里云 ACR 密码/访问凭证 |
| `SERVER_HOST` | `43.136.135.75` |
| `SERVER_USER` | `root` |
| `SSH_PRIVATE_KEY` | 可登录服务器的私钥（与 pm 项目相同的部署私钥即可） |

### 服务器约定

- 部署目录固定 `/opt/HYSG`，需存在 `.env`（`SGHY_PUBLIC_IP` / `SGHY_GM_TOKEN` 等）
- 服务器只 `pull` 镜像，不做本地构建；`/root/.docker/config.json` 需已登录 ACR
  （CI 部署时会再 `docker login` 一次，避免凭证丢失）
- 存档在 Docker 卷 `hysg-server-data`，日志在 `server/logs/`，配置走绑定挂载

手动部署/回滚：

```bash
cd /opt/HYSG
IMAGE_TAG=<commit-sha> /bin/bash scripts/deploy.sh
tail -f /var/log/hysg-server-deploy.log
```

## 本地构建

```bash
docker compose build          # 镜像 tag = .../hysg-server:local
docker compose up -d
```
