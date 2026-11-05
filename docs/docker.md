# Docker 部署（仅后端）

> 完整部署（宿主机 / 客户端 / 备份 / 排错）见 [`deployment.md`](deployment.md)。
> 公网部署（阿里云 ECS）见 [`aliyun.md`](aliyun.md)。

客户端（Windows `player.exe` / Android APK）仍在宿主机或手机上运行；Docker 只跑
`server/` 的 Python 服务端。任何装了 Docker 的机器，复制仓库后即可启动。

## 安全：管理接口令牌

公网部署**必须**设置 `SGHY_GM_TOKEN`，否则 `/api/gm`、`/mail`、`/api/mail/*`
任何人都能访问（发道具、改 VIP）：

```bash
SGHY_GM_TOKEN=$(openssl rand -hex 16) docker compose up -d --build
# 之后调用： /api/gm?action=...&token=<TOKEN>
# 发信台：   http://<IP>:8000/mail?token=<TOKEN>
```

工具链的 `build/ship` 会读取同名环境变量，自动给热重载请求带上 token。

## 前提

- Docker Desktop（Windows）或 Docker Engine（Linux）
- 宿主机保留这些目录（compose 会挂载，不打进镜像）：
  - `env/_work/rt_all/` 配置 Lua
  - `server/sghy_server/config/` 活动/兜底配置
  - `server/data/` 存档
  - `server/logs/` 日志

## 启动

Windows PowerShell：

```powershell
$env:SGHY_PUBLIC_IP = "192.168.1.5"   # 客户端实际访问的宿主机局域网 IP
docker compose up -d --build
docker compose ps                      # 应显示 healthy
```

Linux/macOS：

```bash
SGHY_PUBLIC_IP=192.168.1.5 docker compose up -d --build
```

也可以把 `SGHY_PUBLIC_IP=192.168.1.5` 写进仓库根 `.env` 文件，后续直接
`docker compose up -d`。

- TCP `2724`：游戏协议
- HTTP `8000`：`/api/getserverlist`、`/api/getannounce`、`/mail`、`/api/gm`
- `SGHY_PUBLIC_IP` 决定 serverlist 下发给客户端的地址；局域网玩必须填宿主机 IP

## 配置与热重载

| 内容 | 在宿主机改哪里 | 生效方式 |
|---|---|---|
| Excel 配置（武将/关卡/技能…） | `config_src/*.xlsx` → `python tools/config_tool.py build --no-reload` | 写入 `env/_work/rt_all`（容器只读挂载）后调用重载接口 |
| 活动参数 | `server/sghy_server/config/activities.json` | 调用重载接口 |
| 截图/邮件页 | `http://<IP>:8000/mail` | — |

重载接口（容器内服务端会清缓存并重新预热）：

```bash
curl "http://192.168.1.5:8000/api/gm?action=reload_config"
```

> 玩家数值在**重新登录**时按新配置重算。

## 数据与日志

- **存档在 Docker 原生卷 `hysg-server-data`**（SQLite WAL 不能跑在 Windows 绑定挂载上，
  否则启动即报 `disk I/O error`）
- 首次启动会自动把宿主机 `server/data/sghy.db*` 只读挂载到 `/seed-db` 并**导入一次**；
  之后容器只读写卷，宿主机 `server/data` 不再更新
- 日志：`server/logs/server.log`（绑定挂载，宿主机可直接看）
- 空库（卷不存在）时还会从镜像内 `/seed/player_data.json` 迁移一次旧存档

> ⚠️ 不要混用两种运行方式：`env\start_server.bat`（宿主机直跑）读的是
> `server/data/sghy.db`，与容器卷是两份独立存档。切换前先按下面命令导出/导入。

备份（先停服务，保证 WAL 一致）：

```bash
docker compose stop
docker run --rm -v hysg-server-data:/data -v "${PWD}:/backup" \
  alpine sh -c "tar czf /backup/hysg-data.tgz -C /data ."
docker compose start
```

恢复：

```bash
docker compose stop
docker run --rm -v hysg-server-data:/data -v "${PWD}:/backup" \
  alpine sh -c "rm -rf /data/* && tar xzf /backup/hysg-data.tgz -C /data"
docker compose start
```

## 常用命令

```bash
docker compose logs -f server       # 跟踪日志
docker compose restart server
docker compose down                 # 停止（存档在卷里，不会丢）
docker compose down -v              # 连存档卷一起删（下次启动重新从 /seed-db 导入）
docker compose up -d --build        # 更新代码后重建
```

## 不包含在 Docker 里的部分

- Windows 客户端 `player.exe`（原生程序，需在 Windows 运行）
- Android 重打包/安装（依赖 Windows `luac.exe`、`jarsigner`、`adb`）
- 配置 Excel 工具（在宿主机用系统 Python 运行 `tools/config_tool.py`）
