# 部署指南

本仓库包含三部分，可分别部署：

| 组件 | 位置 | 运行环境 |
|---|---|---|
| 服务端 | `server/`（Python 3.13 + asyncio，TCP 2724 / HTTP 8000） | 宿主机或 Docker |
| 客户端 | `env/sghy_run/` + `env/quick_src/.../player.exe`（Windows）或 `build/youguTV_hysg.apk`（Android） | Windows / Android |
| 配置工具 | `tools/config_tool.py`（Excel → Lua，可选重打包） | Windows（需 `luac.exe`/`jarsigner`/`adb`） |

推荐组合：**服务端用 Docker（或宿主机 Python），客户端仍在目标机器上运行**。

## 1. 环境要求

- 服务端
  - Python 3.13，依赖见 `server/requirements.txt`
  - 或 Docker Desktop / Docker Engine + Compose v2
- 客户端
  - Windows：`player.exe`（仓库已带），或按 `env/build_player.bat` 重编
  - Android：安装 `build/youguTV_hysg.apk`，或自行重打包
- 网络
  - TCP `2724`（游戏协议）、HTTP `8000`（服务器列表/公告/邮件页/GM）
  - 局域网客户端需能访问宿主机 IP；防火墙放行上述端口
  - 客户端连接地址由 `SGHY_PUBLIC_IP` 决定（serverlist 下发），不是写死在包里

## 2. 方式一：Docker（推荐）

```powershell
# 仓库根目录，PowerShell
$env:SGHY_PUBLIC_IP = "192.168.1.5"   # 改成宿主机局域网 IP
docker compose up -d --build
docker compose ps                      # 等待 healthy
```

```bash
# Linux/macOS
SGHY_PUBLIC_IP=192.168.1.5 docker compose up -d --build
```

验证：

```bash
curl http://<IP>:8000/api/getserverlist     # ip1/game_port 应指向宿主机
curl http://<IP>:8000/api/getannounce
```

细节（卷、备份恢复、热重载、WAL 注意事项）见 [`docker.md`](docker.md)。
公网服务器部署见 [`aliyun.md`](aliyun.md)（含安全组、存档迁移、GM 令牌）。

## 3. 方式二：宿主机直跑

```bash
cd HYSG
python -m pip install -r server/requirements.txt
cd server
python -m sghy_server          # 或双击 env/start_server.bat
```

- 服务端默认读：
  - 配置：`env/_work/rt_all/`（可用 `SGHY_CONFIG_DIR` 覆盖）
  - 存档：`server/data/sghy.db`（`SGHY_DB_PATH`）
  - 日志：`server/logs/`（`SGHY_LOG_DIR`）
- 客户端（Windows）：先双击 `env/start_server.bat` 起服务端，再双击 `env/start_client.bat` 起客户端

## 4. 客户端连接

- PC 客户端直接连服务端：确认 `SGHY_PUBLIC_IP` 为宿主机局域网 IP
- Android：
  1. 重打包时烧入 HTTP 地址：`python tools/android_repack.py --server-host <IP>:8000 --install --launch`
  2. 客户端先请求 `http://<IP>:8000/api/getserverlist`，再按返回的 `ip1:game_port` 连 TCP
- 端口不通时检查：宿主机防火墙、Docker 端口映射、设备与宿主机是否同一网段

## 5. 配置更新与生效

| 改什么 | 操作 | 生效方式 |
|---|---|---|
| 武将/装备/技能/关卡/军旗/签到/活动入口 | 改 `config_src/*.xlsx` → `python tools/config_tool.py validate` → `ship`（含重打包）或 `build`（仅服务端） | 客户端配置需重打包；服务端数值需重新登录 |
| 服务端活动参数 | 改 `server/sghy_server/config/activities.json`，或通过 `activity.xlsx` 后 `build --tables activity` | 热重载 + 重新登录 |
| 客户端补丁 | 改 `env/sghy_run/override/*.lua` 或 `android/main_android.lua` | `ship` 重新打包；Windows player 直接读 |

热重载：

```bash
curl "http://<IP>:8000/api/gm?action=reload_config"
```

输出 `配置已重载（成长表=ok, 武将=…, 关卡布阵=…）` 即成功；玩家需重新登录刷新属性。

## 6. 存档、日志与备份

- 宿主机直跑：`server/data/sghy.db`（含 `-wal/-shm`）、`server/logs/server.log`
- Docker：存档在卷 `hysg-server-data`（首次启动从 `server/data` 导入一次），日志在宿主机 `server/logs/`
- **两种方式不要混用**，切换前先导出存档。Docker 备份/恢复命令见 `docs/docker.md`
- SQLite 使用 WAL：备份前先停服务，避免拷到半写状态

### 从线上拉回本机（玩家进度回流）

线上玩出来的进度在 Docker 卷里，本机 `server/data/sghy.db` 不会自动更新。
用脚本一键回流（容器**不用停**，内部用 `sqlite3` backup API 取一致性快照）：

```bash
python tools/sync_db_from_server.py              # 拉取并替换本机存档
python tools/sync_db_from_server.py --dry-run    # 只下载校验，不动本机
python tools/sync_db_from_server.py --host 1.2.3.4 --user ubuntu
```

脚本流程：容器内 `backup` 快照 → `docker cp` → `scp` 回本机 → 旧档备份到
`server/data/_backup/` → 替换 `sghy.db` → **删掉 `-wal/-shm`**。它会打印玩家清单
和表行数，可肉眼核对。

**为什么不能直接 scp 卷里的 `sghy.db`**：容器是 WAL 模式，未 checkpoint 的提交都在
`sghy.db-wal`（实测 4MB，主库才 644KB）。只拷主库会丢最新进度；而把旧 `-wal`
留在新库旁边会直接污染数据。

本机 2724 有监听时脚本会拒绝执行（防止热替换正在用的库），确认要覆盖加 `--force`。

> 本机若用 Docker 跑服务端，替换这个文件**无效** —— 容器读的是卷
> `hysg-server-data`。回流只对宿主机直跑（`env/start_server.bat`）生效。

配置不用这样拉：线上 `env/_work/rt_all` 和 `server/sghy_server/config` 是绑定挂载，
直接 `rsync -av root@<IP>:/opt/HYSG/env/_work/rt_all/ env/_work/rt_all/` 即可；
两边一致时（1486 个文件 md5 全同）就不必同步。

## 7. 升级与回滚

```bash
git pull
# Docker
docker compose up -d --build
# 宿主机
pip install -r server/requirements.txt
```

- 配置回滚：`python tools/config_tool.py restore --last`（恢复到上次 build 前快照）
- 存档回滚：用备份 JSON/DB 覆盖后重启
- 镜像回滚：保留旧 `hysg-server:<tag>` 或重新 `docker compose build`

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| 客户端卡在登录/连接超时 | `SGHY_PUBLIC_IP` 不是设备可达 IP；检查 2724 端口与防火墙 |
| Docker 启动即 `sqlite3.OperationalError: disk I/O error` | 存档放在 Windows 绑定挂载上；改用原生卷（本仓库 compose 已处理） |
| `No module named google.protobuf` / 版本错误 | 用 `pip install -r server/requirements.txt`；别用系统旧 Python |
| 控制台中文乱码/编码报错 | 设 `PYTHONIOENCODING=utf-8` |
| 改配置后数值没变 | 服务端需重载接口；玩家需重新登录；客户端表需重新打包 |
| `address already in use` | 2724/8000 被占用（宿主机服务端或旧容器），先停掉其一 |
| adb 安装失败 | 确认模拟器已启动、`adb devices` 有设备；`tools/android_repack.py` 默认 adb 路径可改 |

## 9. 目录速查

```
server/          服务端（Dockerfile / requirements.txt / sghy_server / data / logs / config）
env/sghy_run/    客户端运行目录（scripts / override / res / localData）
env/_work/rt_all 配置 Lua（服务端与客户端同源）
config_src/      Excel 工作簿（编辑介质，不入库）
config_data/     配置规范化源（CSV + schema，入库）
tools/           config_tool.py、android_repack.py、unluac.jar
docs/            部署与配置管线文档
```
