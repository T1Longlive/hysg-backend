"""存档门面：把 core 的 PLAYER_DATA dict 变成「有真实持久化的缓存」。

- start()：建表 → 有库读库（空库则从 JSON 一次性迁移）→ 返回数据 dict
- mark_dirty()：替代 core.save_player_data 的 29 处调用点（无感切换）
- 延时批量：标记后 SGHY_FLUSH_DELAY 秒合并一次落库，单事务 diff upsert
- stop()：退出前最终 flush

并发安全性：业务函数全是同步的（dispatch 内无 await），dict 变更对事件循环
原子；flush 先深拷贝快照再写库，不会读到改到一半的状态。
"""

import asyncio
import copy
import os

from .db import Database
from .repo import PlayerRepository


class SaveStore:
    def __init__(self, db_path: str, player_id: int,
                 json_path: str | None = None, flush_delay: float = 1.5):
        self.player_id = player_id
        self.json_path = json_path
        self.flush_delay = flush_delay
        self.repo = PlayerRepository(Database(db_path))
        self._data: dict | None = None
        self._dirty = False
        # 上次成功落库的 heroes/equips 快照（只留这两个大集合，512 武将时
        # 全表 diff 要 34.7ms；对比内存快照后只写变更行，~5ms）
        self._last = {}
        self._flush_task: asyncio.Task | None = None
        self._flushing = False      # 正在写库（flush_soon 此时不抢任务）
        self._soon = False          # flush_soon 请求的"尽快落库"标记
        self._suspended = False     # 账号已注销：停落库，直到下一次 switch_player
        self._closed = False
        self.stats = {"flushes": 0, "migrations": 0}

    # ---- 生命周期 ----
    async def start(self) -> dict:
        await self.repo.db.init()
        data = await self.repo.load(self.player_id)
        if data is None:
            data = await self._migrate_or_seed()
        self._data = data
        return data

    async def _migrate_or_seed(self) -> dict:
        """空库：优先从 JSON 迁移（**一次性**）；没有 JSON 则由调用方给种子。

        ★ 2026-10-28：迁移成功后把 JSON 改名归档（*.migrated-YYYYmmdd-HHMMSS），
          并把文件修改时间打进日志。原因：旧实现在 DB 被删/损坏时会**静默**
          重新 import 那份可能很旧的 JSON → 表现为"进度莫名回退"。
          改名后同一份 JSON 只会被迁移一次，回退不会再无声发生。
        """
        if self.json_path and os.path.exists(self.json_path):
            import datetime as _dt
            _mtime = _dt.datetime.fromtimestamp(os.path.getmtime(self.json_path))
            data = await self.repo.import_json(self.player_id, self.json_path)
            self.stats["migrations"] += 1
            print("=" * 70, flush=True)
            print("[store] !! 空库：已从 JSON 迁移存档（**仅此一次**）", flush=True)
            print("        文件: %s" % self.json_path, flush=True)
            print("        修改时间: %s  heroes=%d items=%d equips=%d"
                  % (_mtime.strftime("%Y-%m-%d %H:%M:%S"),
                     len(data.get("heroes", {})), len(data.get("items", {})),
                     len(data.get("equips", {}))), flush=True)
            print("        !! 请确认这份 JSON 是最新的——若它很旧，本次启动等于回退。",
                  flush=True)
            print("=" * 70, flush=True)
            try:
                _stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                _arch = "%s.migrated-%s" % (self.json_path, _stamp)
                os.replace(self.json_path, _arch)
                print("[store] JSON 已归档为 %s（今后不再自动迁移）" % _arch, flush=True)
            except Exception as _e:
                print("[store] !! JSON 归档失败（%r）——下次空库仍会再次迁移，请手动改名"
                      % _e, flush=True)
            return data
        # 没有 JSON：建一个空玩家行，数据由调用方（core 的 import 期种子）提供
        await self.repo.save(self.player_id, {"player": {}, "heroes": {},
                                              "items": {}, "equips": {},
                                              "fate": {}, "levels": {},
                                              "level_records": {}})
        print("[store] 空库且无 JSON -> 已建空玩家行（等调用方种子）", flush=True)
        return {"player": {}, "heroes": {}, "items": {}, "equips": {},
                "fate": {}, "levels": {}, "level_records": {}}

    async def stop(self):
        self._closed = True
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.flush(force=True)
        await self.repo.db.dispose()

    # ---- 多角色切换（2026-10-29 账号隔离） ----
    async def switch_player(self, player_id: int) -> dict:
        """切到另一角色的存档：先 flush 当前，再载入目标。

        返回目标角色的 PLAYER_DATA 形状 dict。调用方负责之后把 core 全局
        （PLAYER_DATA 原地 clear+update、LEVEL_RECORDS 别名、CHAR_ID）重绑。
        目标角色不存在抛 KeyError。

        ★ 必须**原地** clear+update 到现有 self._data（2026-10-30 修）：
          旧实现 `self._data = data` 换了对象，而 session 侧拿到 data 后调
          `core.apply_player_data(data)` 只 clear+update 旧的 PLAYER_DATA ——
          于是 store 落库写的是"登录那一刻的快照"（自增 dict），业务改的是
          另一个 dict。两边只有**登录时就已存在的嵌套 dict** 共享引用，凡是
          之后 setdefault 新建的顶层键（activity / mission_achieve / mails …）
          全部只活在内存 → 充值累计停在旧值、任务领取状态回滚、重登即丢。
          用户实测症状："买了好几个 648，累计充值还停在 198"（round_pay 没落库，
          而 player 里的 pay_money 因为共享嵌套 dict 反而写进去了）。
        """
        if player_id == self.player_id and self._data is not None and not self._suspended:
            return self._data
        self._dirty = True
        await self.flush(force=True)          # 旧档先安全落库
        data = await self.repo.load(player_id)
        if data is None:
            raise KeyError("player %s 不在库中" % player_id)
        if self._data is None:
            self._data = data
        else:
            self._data.clear()
            self._data.update(data)
        self.player_id = player_id
        self._dirty = False
        self._last = {}                       # 基线清空 → 下次落库按全量
        self._suspended = False               # 新档已接管，恢复落库
        return self._data

    async def create_player(self, player_id: int, account_id: int, data: dict):
        """建新角色：整份初始存档落库 + 绑定账号。"""
        await self.repo.save(player_id, data)
        await self.repo.player_bind_account(player_id, account_id)

    async def discard_current(self):
        """账号注销：取消待落库任务并暂停写库，直到下一次 switch_player。

        不清理 PLAYER_DATA（其它模块持有该 dict 引用，清空会让它们读到空结构），
        只断开"内存 → 库"的写回路径；下次载入新角色时 clear+update 覆盖并恢复。
        """
        self._suspended = True
        self._dirty = False
        self._last = {}
        t = self._flush_task
        self._flush_task = None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # ---- 数据访问 ----
    @property
    def data(self) -> dict:
        return self._data

    def set_data(self, data: dict):
        """替换底层 dict（如从 core 的 JSON 加载结果接管）。"""
        self._data = data

    # ---- 脏标记 / 刷库 ----
    def mark_dirty(self):
        if self._closed or self._suspended:
            return
        self._dirty = True
        self._schedule()

    def _schedule(self):
        if self._closed:
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._debounced_flush())

    def flush_soon(self):
        """关键操作立即落库（不等防抖窗口）。

        充值 / 发邮件 / 装备养成这类"丢了要挨骂"的操作，dispatch 完就调它：
        仍然走同一把 flush（单事务 diff upsert），只是把等待时间降到 0。

        ★ 必须**取消**已在等防抖窗口的任务：否则 mark_dirty 排的那个 0.5s 任务
          还 pending，这里判断"任务未完成"就直接跳过 → 等于没立即落库
          （2026-10-28 单测抓到）。正在写库（_flushing）时不取消，等它写完。
        """
        if self._closed or self._suspended:
            return
        self._dirty = True
        self._soon = True
        t = self._flush_task
        if t is not None and not t.done():
            if self._flushing:
                # 正在写库：不动它。本次变更会在写完后由下一轮 mark_dirty/flush 带走
                return
            t.cancel()                 # ★ cancel 不是立刻 done()，必须显式清引用
            self._flush_task = None
        self._flush_task = asyncio.create_task(self._flush_immediately())

    async def _flush_immediately(self):
        await asyncio.sleep(0)      # 让当前 dispatch 先跑完再落库
        # 期间可能又有变更 → 循环到干净（上限 3 轮防死循环）
        for _ in range(3):
            self._soon = False
            await self.flush()
            if not self._soon:
                break

    async def _debounced_flush(self):
        try:
            await asyncio.sleep(self.flush_delay)
            await self.flush()
        except asyncio.CancelledError:
            raise

    def _diff_big_collections(self, snapshot: dict) -> dict:
        """算出 heroes/equips 里内容变化（含新增/删除）的 id 集合。

        返回 {"heroes": set|None, "equips": set|None}；
        None = 没有历史快照（首次落库，按全量处理）。
        """
        out = {}
        for key in ("heroes", "equips"):
            new = snapshot.get(key) or {}
            old = self._last.get(key)
            if old is None:
                out[key] = None          # 首次 → 全量
                continue
            ids = set()
            for k, v in new.items():
                if old.get(k) != v:      # dict 深比较，字段级等价
                    ids.add(k)
            for k in old:
                if k not in new:         # 被删除的行也要写（repo 会 delete）
                    ids.add(k)
            out[key] = ids
        return out

    async def flush(self, force: bool = False):
        if self._suspended:
            return
        if not self._dirty and not force:
            return
        if self._data is None:
            return
        snapshot = copy.deepcopy(self._data)
        dirty = self._diff_big_collections(snapshot)
        self._flushing = True          # flush_soon 见到它在写库就不抢任务
        try:
            await self.repo.save(self.player_id, snapshot, dirty)
            self._dirty = False
            self.stats["flushes"] += 1
            # 落库成功才更新基线（失败时下次仍按旧基线重算，不会漏写）
            self._last = {k: (snapshot.get(k) or {}) for k in ("heroes", "equips")}
        except Exception as e:
            # 落库失败不崩服务：保持 dirty，下次重试
            print("[store] flush 失败（%r），保持 dirty 待重试" % e, flush=True)
        finally:
            self._flushing = False
