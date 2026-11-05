"""服务器配置。端口/路径均可用环境变量覆盖。"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))   # 仓库根（.../HYSG）

# TCP 游戏服务（客户端 serverlist 返回的 game_port 指向这里）
TCP_HOST = os.environ.get("SGHY_TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.environ.get("SGHY_TCP_PORT", "2724"))

# HTTP 网关（服务器列表 / 公告 / 热更新配置 / GM 接口）
HTTP_HOST = os.environ.get("SGHY_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("SGHY_HTTP_PORT", "8000"))

SERVER_ID = int(os.environ.get("SGHY_SERVER_ID", "10001"))
SERVER_NAME = os.environ.get("SGHY_SERVER_NAME", "本地测试服")

# 客户端连接用的对外地址（serverlist 下发给客户端）
PUBLIC_IP = os.environ.get("SGHY_PUBLIC_IP", "127.0.0.1")

# GM / 发信台管理接口令牌：非空时，/api/gm、/mail、/api/mail/* 必须带
# ?token=... 或请求头 X-GM-Token；公网部署必须设置。
GM_TOKEN = os.environ.get("SGHY_GM_TOKEN", "")

LOG_DIR = os.environ.get("SGHY_LOG_DIR", os.path.join(_ROOT, "server", "logs"))

# SQLite 存档（真实持久化；空库首次启动自动从 SGHY_PLAYER_DATA 的 JSON 迁移）
DB_PATH = os.environ.get(
    "SGHY_DB_PATH", os.path.join(_ROOT, "server", "data", "sghy.db"))

# 脏数据延时批量落库间隔（秒）：业务 29 处写点全部合并成单事务
# 落库防抖窗口（秒）：1.5 → 0.5（2026-10-28）。强杀进程最多丢最后 0.5s 改动，
# 原先 1.5s 窗口实测丢过邮件（#1~#3）。需要更保守可设 0.2。
FLUSH_DELAY = float(os.environ.get("SGHY_FLUSH_DELAY", "0.5"))

# ---- 账号隔离（2026-10-29）----
# 旧档（players.id=1001）归属的账号。启动时幂等绑定；该账号 hash 为空，
# 首次用它登录时输入的密码即其密码。
LEGACY_OWNER_ACCOUNT = os.environ.get("SGHY_LEGACY_OWNER", "19881695096")
# 设 1 可跳过账号层（应急回滚：登录不鉴权、恒回旧行为）——会话层自动走旧路径
LEGACY_SINGLE = os.environ.get("SGHY_LEGACY_SINGLE", "0") == "1"
