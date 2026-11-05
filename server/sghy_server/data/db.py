"""数据库引擎与会话管理（异步 SQLite）。"""

import os

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)

from .models import Base, Equip, Hero, ItemStack


def make_engine(db_path: str):
    # check_same_thread=False：asyncio 单线程事件循环，跨 task 使用同一连接安全
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", echo=False, future=True)

    # ★ 2026-10-28：切 WAL + synchronous=NORMAL。
    #   · WAL：写不阻塞读，崩溃/断电时只有"未提交事务"风险，主库不会被写坏一半
    #     （原 journal_mode=delete，断电时最后一批事务更易损坏）
    #   · synchronous=NORMAL：WAL 下等价于"每次 checkpoint 才 fsync"，
    #     落库延迟从 ~10ms 降到 ~1ms，且断电最多丢最后一个事务（可接受）
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):        # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
        finally:
            cur.close()

    return engine


class Database:
    def __init__(self, db_path: str):
        # SQLite 不会自建父目录（删库重跑会踩到）
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.db_path = db_path
        self.engine = make_engine(db_path)
        self.sessionmaker = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False)

    async def init(self):
        async with self.engine.begin() as conn:
            await self._migrate_composite_pk(conn)
            await conn.run_sync(Base.metadata.create_all)

    # ---- 2026-10-29 账号隔离：heroes/equips/item_stacks 复合主键 (player_id, id) ----
    _COMPOSITE_PK_TABLES = ("heroes", "equips", "item_stacks")

    async def _migrate_composite_pk(self, conn):
        """旧库（实例 id 全局唯一主键）→ 复合主键，自动重建一次。

        判据：PRAGMA table_info 的 pk 列集合。老库 {"id"}，目标 {"player_id","id"}。
        实例 id 本来就全局唯一（旧约束保证），直接整表搬运零风险；幂等。
        ★ 断点续迁：SQLite 这批 DDL/DML 在 aiosqlite 上**不保证同事务原子**
          （首跑实测：heroes/equips 新表建好、数据还留在 *_old_pk）。所以每张表
          先看 *_old_pk 在不在——在就先续跑（搬数据→校验行数→删旧表）。
        """
        for name in self._COMPOSITE_PK_TABLES:
            table = {"heroes": Hero, "equips": Equip, "item_stacks": ItemStack}[name]
            old = "%s_old_pk" % name

            if await self._table_exists(conn, old):
                await self._finish_pk_move(conn, name, old, table)

            res = await conn.exec_driver_sql("PRAGMA table_info(%s)" % name)
            rows = res.fetchall()
            if not rows:
                continue                       # 表不存在 -> create_all 建
            pk_cols = {r[1] for r in rows if r[5] > 0}
            if pk_cols == {"player_id", "id"}:
                continue                       # 已是新 schema
            print("[db] 迁移 %s: 主键 (%s) -> (player_id, id) ..." % (name, ",".join(
                sorted(pk_cols))), flush=True)
            await conn.exec_driver_sql(
                "ALTER TABLE %s RENAME TO %s" % (name, old))
            # ★ 旧表的命名索引（ix_xxx_player_id 等）跟着改名表走，名字不变——
            #   不先 DROP 的话新建表 CREATE INDEX 会撞名（首跑实测）。自动名字
            #   的 autoindex 不用管。
            idx_res = await conn.exec_driver_sql(
                "PRAGMA index_list(%s)" % old)
            for idx in idx_res.fetchall():
                iname = idx[1]
                if str(iname).startswith("sqlite_autoindex"):
                    continue
                await conn.exec_driver_sql('DROP INDEX "%s"' % iname)
            await conn.run_sync(
                Base.metadata.create_all, tables=[table.__table__])
            await self._finish_pk_move(conn, name, old, table)

    async def _finish_pk_move(self, conn, name: str, old: str, table):
        """*_old_pk -> 新表：搬运 + 行数校验 + 删旧表（幂等，INSERT 用 OR IGNORE）。"""
        cols = ", ".join(c.name for c in table.__table__.columns)
        await conn.exec_driver_sql(
            "INSERT OR IGNORE INTO %s (%s) SELECT %s FROM %s"
            % (name, cols, cols, old))
        n_new = (await conn.exec_driver_sql(
            "SELECT COUNT(*) FROM %s" % name)).scalar()
        n_old = (await conn.exec_driver_sql(
            "SELECT COUNT(*) FROM %s" % old)).scalar()
        if n_new != n_old:
            raise RuntimeError(
                "[db] %s 迁移行数不一致 new=%s old=%s，保留 *_old_pk 待人工检查"
                % (name, n_new, n_old))
        await conn.exec_driver_sql("DROP TABLE %s" % old)
        print("[db] 迁移 %s 完成（%s 行）" % (name, n_new), flush=True)

    async def _table_exists(self, conn, name: str) -> bool:
        res = await conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='%s'" % name)
        return res.scalar() is not None

    async def dispose(self):
        await self.engine.dispose()
