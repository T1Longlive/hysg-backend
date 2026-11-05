"""账号层：登录鉴权 + 注册表 + 限速（2026-10-29 账号隔离 P1）。

设计：
  · 注册表常驻内存（accounts 表很小），启动时从 repo 一次性载入；登录路径
    全同步（dispatch 无 await 的架构约束不破坏），落库用 asyncio.create_task
    派发（与 store.mark_dirty 的"同步标记 + 异步落库"同款模式）。
  · 密码：hashlib.scrypt（标准库，不引新依赖），随机 16 字节 salt，
    存 "scrypt$n$r$p$salt_hex$hash_hex"。
  · 登录即注册：账号不存在 → 建号（首次输入的密码即注册密码）；账号存在
    但 hash 为空（迁移出来的旧行）→ 首次登录补设密码。
  · 游客：IsVisitor=1 或账号为空 → 按 Device 派生固定账号 guest_<hash8>，
    同一台设备永远同一个号（避免每次登录换新号）。
  · 限速：同账号 60 秒窗口内密码错 5 次 → 拒绝登录（内存计数，重启清零）。
"""

import asyncio
import hashlib
import hmac
import os
import time

MAX_CHARS_PER_ACCOUNT = 3          # 每账号角色上限（客户端选角面板够用）
FAIL_WINDOW = 60.0                 # 失败计数窗口（秒）
FAIL_LIMIT = 5                     # 窗口内允许的最大失败次数

# ---- 内存注册表：{username: {"id": int, "hash": str}} ----
_ACCOUNTS: dict = {}
_repo = None                       # PlayerRepository，启动时注入
_loop = None                       # 事件循环（create_task 用）

# ---- 失败计数：{username: [timestamps]} ----
_FAILS: dict = {}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                       n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$%s$%s" % (salt.hex(), h.hex())


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        _tag, n, r, p, salt_hex, hash_hex = stored.split("$")
        h = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                           n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(h.hex(), hash_hex)
    except Exception:
        return False


def _guest_username(device: str) -> str:
    d = (device or "").strip() or "unknown-device"
    return "guest_" + hashlib.sha1(("yougu:" + d).encode("utf-8")).hexdigest()[:12]


def _persist(coro):
    """异步落库派发（失败只打日志，不阻塞登录路径）。"""
    async def _run():
        try:
            await coro
        except Exception as e:
            print("[account] 落库失败（%r）" % e, flush=True)
    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass


async def load_registry(repo) -> int:
    """启动时载入账号注册表。返回账号数。"""
    global _repo, _loop
    _repo = repo
    _loop = asyncio.get_running_loop()
    rows = await repo.accounts_all()
    _ACCOUNTS.clear()
    for name, info in rows.items():
        _ACCOUNTS[name] = {"id": info["id"], "hash": info["hash"] or ""}
    print("[account] 注册表就绪：%d 个账号" % len(_ACCOUNTS), flush=True)
    return len(_ACCOUNTS)


def _rate_limited(username: str) -> bool:
    now = time.monotonic()
    fails = [t for t in _FAILS.get(username, []) if now - t < FAIL_WINDOW]
    _FAILS[username] = fails
    return len(fails) >= FAIL_LIMIT


def _record_fail(username: str):
    _FAILS.setdefault(username, []).append(time.monotonic())


def _clear_fails(username: str):
    _FAILS.pop(username, None)


def _persist_account(username: str, pw_hash: str) -> int:
    """内存登记 + 异步落库。id 由内存自增序号保证唯一（表 autoincrement 对齐）。"""
    aid = (max((r["id"] for r in _ACCOUNTS.values()), default=0)) + 1
    _ACCOUNTS[username] = {"id": aid, "hash": pw_hash}
    if _repo is not None:
        _persist(_repo.account_add(username, pw_hash))
    return aid


def login_or_register(account: str, password: str,
                      is_visitor: bool, device: str):
    """登录主入口。返回 (account_dict, err)。

    account_dict: {"id": int, "username": str}；err 为 None 表示成功。
    语义：
      · 游客 / 账号空 → 按 Device 派生游客账号（存在即用，不存在即建）
      · 普通账号不存在 → 登录即注册（首次密码即注册密码）
      · 账号存在 hash 空（迁移旧行）→ 首次登录补设密码
      · 密码错 → 计失败（限速 5 次/分钟）
    """
    username = (account or "").strip()
    if is_visitor or not username:
        username = _guest_username(device)
        password = password or ("guest-" + username)   # 游客密码固定派生（本地无意义，占位）

    if _rate_limited(username):
        return None, "尝试次数过多，请 1 分钟后再试"

    row = _ACCOUNTS.get(username)
    if row is None:
        # ---- 登录即注册 ----
        if not password:
            return None, "请输入密码"
        aid = _persist_account(username, hash_password(password))
        _clear_fails(username)
        return {"id": aid, "username": username}, None

    # ---- 已有账号：校验 / 补设 ----
    if not row["hash"]:
        # 迁移出来的旧行（如旧档绑定账号），首次登录补设密码
        if not password:
            return None, "该账号首次使用，请输入密码完成设置"
        row["hash"] = hash_password(password)
        if _repo is not None:
            _persist(_repo.account_set_hash(row["id"], row["hash"]))
        _clear_fails(username)
        return {"id": row["id"], "username": username}, None

    if not password or not verify_password(password, row["hash"]):
        _record_fail(username)
        return None, "账号或密码错误"

    _clear_fails(username)
    return {"id": row["id"], "username": username}, None


def touch_login(account_id: int):
    if _repo is not None:
        _persist(_repo.account_touch(account_id))


def is_known_username(username: str) -> bool:
    return (username or "").strip() in _ACCOUNTS


def forget_account(username: str):
    """注销：从内存注册表移除账号并清失败计数。"""
    name = (username or "").strip()
    _ACCOUNTS.pop(name, None)
    _FAILS.pop(name, None)


async def ensure_legacy_binding(repo, owner_username: str, player_id: int = 1001):
    """旧档归属（幂等，启动时跑）：把 players.id=1001 绑到指定账号。

    · 账号不存在 → 建行（hash 空，首次登录补设密码）
    · 角色 account_id 已有值 → 不动（不覆盖任何归属决定）
    """
    from sqlalchemy import select as _select
    from ..data.models import Player

    rows = await repo.accounts_all()
    info = rows.get(owner_username)
    if info is None:
        aid = await repo.account_add(owner_username, "")
        print("[account] 旧档归属账号 %s 不存在 -> 已建（id=%s，首次登录设密码）"
              % (owner_username, aid), flush=True)
    else:
        aid = info["id"]
    async with repo.db.sessionmaker() as s:
        row = (await s.execute(
            _select(Player).where(Player.id == player_id))).scalar_one_or_none()
        if row is None:
            print("[account] 未找到旧档 players.id=%s，跳过绑定" % player_id, flush=True)
            return
        if row.account_id is not None:
            print("[account] 旧档 %s 已归属账号 %s，不覆盖" % (player_id, row.account_id),
                  flush=True)
            return
    await repo.player_bind_account(player_id, aid)
    print("[account] 旧档 players.id=%s -> 账号 %s（id=%s）绑定完成"
          % (player_id, owner_username, aid), flush=True)
