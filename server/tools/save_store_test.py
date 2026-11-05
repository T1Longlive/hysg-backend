"""存档层增量落库测试（2026-10-25）。

验证 SaveStore 的 dirty-id 增量写：
  · 首次落库全量、之后只写变化的 heroes/equips 行
  · 新增 / 删除 / 修改 三种变更都能正确落库
  · 落库后从 DB 回读的内容与内存完全一致（不能漏写）
  · 顺带记录耗时（512 武将规模）
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sghy_server.data.db import Database
from sghy_server.data.repo import PlayerRepository
from sghy_server.data.store import SaveStore


def _seed_json(path):
    """造一份 200 武将 / 50 装备 / 60 道具的小存档（跑得快）。"""
    data = {
        "player": {"lv": 60, "coin": 1000, "diamond": 100},
        "heroes": {str(i): {"id": i, "type": 10000 + i, "lv": (i % 60) + 1,
                            "grade": i % 9, "position": i if i <= 5 else 0}
                   for i in range(1, 201)},
        "equips": {str(i): {"id": i, "type": 20000 + i, "host": 0,
                            "strengthen": 0, "grade": 0}
                   for i in range(1, 51)},
        "items": {str(i): i * 3 for i in range(1, 61)},
        "levels": {"1001": {"star": 3, "leftTimes": 10, "buyTimes": 0}},
        "level_records": {"1001": {"star": 3}},
        "buy_power": {"day": "2026-10-25", "ep_times": 1},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


async def main():
    tmp = tempfile.mkdtemp(prefix="sghy_store_")
    json_path = os.path.join(tmp, "player.json")
    db_path = os.path.join(tmp, "sghy.db")
    seed = _seed_json(json_path)

    st = SaveStore(db_path, 1, json_path=json_path, flush_delay=999)
    data = await st.start()
    assert len(data["heroes"]) == 200 and len(data["equips"]) == 50

    # ---- 首次：全量落库 ----
    await st.flush(force=True)
    assert st.stats["flushes"] == 1

    # ---- 只改一个武将：dirty 集合应只有 1 个 id ----
    data["heroes"]["7"]["lv"] = 99
    dirty = st._diff_big_collections(data)
    assert dirty["heroes"] == {"7"}, dirty["heroes"]
    assert dirty["equips"] == set(), dirty["equips"]
    await st.flush(force=True)

    # ---- 无变化：dirty 为空 ----
    dirty = st._diff_big_collections(data)
    assert dirty["heroes"] == set() and dirty["equips"] == set(), dirty

    # ---- 新增 + 删除 ----
    data["heroes"]["999"] = {"id": 999, "type": 10999, "lv": 1, "position": 0}
    data["equips"]["88"] = {"id": 88, "type": 20888, "host": 0,
                            "strengthen": 0, "grade": 0}
    del data["heroes"]["3"]
    await st.flush(force=True)

    # ---- 回读一致性（最关键：不能漏写/漏删）----
    repo = PlayerRepository(Database(db_path))
    loaded = await repo.load(1)
    # 库里的行会比内存多出「默认值字段」（awaken/star/avatar...），所以按
    # 「内存的每个字段在库里都存在且相等」比对，行集合（id）必须完全一致。
    for key in ("heroes", "equips", "levels", "level_records"):
        mem = data.get(key) or {}
        dbv = loaded.get(key) or {}
        assert set(dbv) == set(mem), "集合 %s 的 id 集合不一致：多 %s 少 %s" % (
            key, set(dbv) - set(mem), set(mem) - set(dbv))
        for rid, row in mem.items():
            for f, fv in (row or {}).items():
                assert dbv[rid].get(f) == fv, "字段 %s.%s.%s 不一致：库=%r 内存=%r" % (
                    key, rid, f, dbv[rid].get(f), fv)
    assert (loaded.get("items") or {}) == (data.get("items") or {}), "道具数量不一致"
    assert "999" in loaded["heroes"], "新增的武将没落库"
    assert "3" not in loaded["heroes"], "删除的武将没从库里删掉"
    assert len(loaded["heroes"]) == 200, len(loaded["heroes"])

    # ---- 性能（200 武将规模）----
    import time
    data["heroes"]["7"]["lv"] = 100
    t0 = time.perf_counter()
    for i in range(10):
        data["heroes"]["7"]["lv"] = 100 + i
        await st.flush(force=True)
    per = (time.perf_counter() - t0) / 10 * 1000
    print("PASS 增量落库：首次全量 / 单行变更 / 新增 / 删除 / 回读一致")
    print("    改单行 flush 平均 %.1f ms（200 武将规模）" % per)

    await st.stop()
    await repo.db.dispose()
    print("\n全部通过 ✔")


if __name__ == "__main__":
    asyncio.run(main())
