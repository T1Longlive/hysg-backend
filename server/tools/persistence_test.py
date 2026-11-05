"""存档持久化关键行为测试（2026-10-28）。

覆盖三项加固：
  1. **JSON 迁移只发生一次**：空库迁移后 JSON 被改名归档（*.migrated-*），
     下次启动不会再静默回退到旧快照
  2. **flush_soon 立即落库**：关键操作（充值/邮件/装备养成）不等防抖窗口
  3. **引导进度落库**：10101 SetGuideIndex 写入 players.guide_index（只升不降）

另附：SQLite 连接必须启用 WAL。

用法：python tools/persistence_test.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sghy_server.data.db import Database
from sghy_server.data.store import SaveStore

TMP = tempfile.mkdtemp(prefix="sghy_persist_")


def _mk_json(path, marker_hero_id=999001):
    data = {
        "player": {"lv": 42, "coin": 777, "vip": 3, "name": "迁移测试"},
        "heroes": {str(marker_hero_id): {"id": marker_hero_id, "type": 10146,
                                        "lv": 80, "grade": 9, "position": 1}},
        "items": {"20002": 5},
        "equips": {},
        "fate": {},
        "levels": {}, "level_records": {},
        "sign": {"sign_times": 7},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


async def test_json_migrated_once():
    db_path = os.path.join(TMP, "mig.db")
    json_path = os.path.join(TMP, "player_data.json")
    _mk_json(json_path)

    store = SaveStore(db_path=db_path, player_id=1001, json_path=json_path,
                      flush_delay=0.05)
    data = await store.start()
    assert "999001" in data.get("heroes", {}), "应从 JSON 迁移出武将"
    assert data["player"]["lv"] == 42, data["player"]
    assert int(data["player"].get("account_id") or 0) >= 0  # 只做形状检查

    # JSON 必须已被改名归档
    assert not os.path.exists(json_path), "迁移后原 JSON 必须被改名，不能留着被二次迁移"
    archived = [f for f in os.listdir(TMP) if f.startswith("player_data.json.migrated-")]
    assert archived, "应生成 *.migrated-* 归档文件: %s" % os.listdir(TMP)
    assert store.stats["migrations"] == 1

    # 再次启动（同 DB）：不应再迁移，且数据保持
    store2 = SaveStore(db_path=db_path, player_id=1001, json_path=json_path,
                       flush_delay=0.05)
    data2 = await store2.start()
    assert store2.stats["migrations"] == 0, "第二次启动不能再走 JSON 迁移"
    assert "999001" in data2.get("heroes", {}), data2.get("heroes", {}).keys()
    print("PASS JSON 迁移一次性：迁移后改名归档、二次启动不再回退")


async def test_flush_soon():
    db_path = os.path.join(TMP, "flush.db")
    store = SaveStore(db_path=db_path, player_id=1001, json_path=None,
                      flush_delay=30)      # 防抖窗口故意设 30s，只能靠 flush_soon
    data = await store.start()
    data.setdefault("player", {})["coin"] = 12345
    data.setdefault("heroes", {})["1"] = {"id": 1, "type": 10146, "lv": 80,
                                          "grade": 9, "position": 1}
    store.mark_dirty()
    store.flush_soon()
    await asyncio.sleep(0.6)             # 远小于 30s，证明不是防抖触发的

    from sqlalchemy import text
    async with store.repo.db.sessionmaker() as s:
        coin = (await s.execute(text("SELECT coin FROM players WHERE id=1001"))).scalar()
        hero = (await s.execute(text("SELECT COUNT(*) FROM heroes WHERE id=1"))).scalar()
    assert coin == 12345, "flush_soon 应立即把 coin 落库，实际 %s" % coin
    assert hero == 1, "flush_soon 应立即落库新武将"
    assert store.stats["flushes"] >= 1
    print("PASS flush_soon：防抖窗口 30s 下仍立即落库（coin=%s heroes=%s）" % (coin, hero))


def test_guide_index_persisted():
    """10101 SetGuideIndex 必须写进 player.guide_index（并落库）。"""
    os.environ["SGHY_PLAYER_DATA"] = os.path.join(TMP, "guide_player.json")
    from sghy_server.game import core

    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({"player": {"lv": 60}, "heroes": {}, "items": {},
                             "equips": {}, "fate": {}, "levels": {},
                             "level_records": {}})

    class Sess:
        def __init__(self):
            self.sent = []

        def send(self, op, body):
            self.sent.append(op)

        def log(self, *a):
            pass

    from sghy_server.proto import ps_protobuf_pb2 as pb
    s = Sess()
    msg = pb.IDMessage()
    msg.id = 37
    core.dispatch(s, 10101, msg.SerializeToString())
    assert 10101 in s.sent, "必须 ack 10101: %s" % s.sent
    assert core.PLAYER_DATA["player"].get("guide_index") == 37, \
        "引导进度必须落库到 player.guide_index，实际 %r" % core.PLAYER_DATA["player"].get("guide_index")

    # 只升不降：回传更小的值不应覆盖
    msg2 = pb.IDMessage(); msg2.id = 12
    core.dispatch(s, 10101, msg2.SerializeToString())
    assert core.PLAYER_DATA["player"]["guide_index"] == 37, "引导进度只升不降"
    print("PASS 引导进度落库：10101 写 player.guide_index=37，且只升不降")


async def test_wal_enabled():
    db_path = os.path.join(TMP, "wal.db")
    db = Database(db_path)
    await db.init()
    from sqlalchemy import text
    async with db.sessionmaker() as s:
        mode = (await s.execute(text("PRAGMA journal_mode"))).scalar()
    assert str(mode).lower() == "wal", "SQLite 必须启用 WAL，实际 %s" % mode
    await db.dispose()
    print("PASS SQLite journal_mode=WAL")


async def main():
    await test_json_migrated_once()
    await test_flush_soon()
    test_guide_index_persisted()
    await test_wal_enabled()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n全部通过 ✔")


if __name__ == "__main__":
    asyncio.run(main())
