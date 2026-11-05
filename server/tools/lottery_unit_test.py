"""招募 / 寻宝 单元测试（fake session，不经 TCP）。

覆盖：
  1. 5041 记录下发（10 条，nextFreeTime 是剩余秒数）
  2. 101 首次免费单抽 → 2003 池（4★），消耗首抽、进入 24h 冷却
  3. 104 首次十连 → 10 个武将，最后一抽必为 1004 池的 6★
  4. 20 抽保底：元宝线跨 101/104/129 共用，第 20 抽落在 1030 池（5★）
  5. 129 招贤令十连：扣 10 个 20010 → 451，产出 10 个武将
  6. 102 首次免费单抽 → 固定张星彩 2★
  7. 201 首次免费寻宝 → 固定 31111 装备（403 + 5040 AwardResult）
  8. 203 首次十连寻宝 → 10 条 AwardResult，含 3320 紫色装备
  9. 104 折扣：2590 × 50% = 1295，折扣次数 -1
 10. 资源不足 → 只回 5002，不发 476

★ 必须在 import core 之前隔离 SGHY_PLAYER_DATA（否则会覆盖真实存档）。
"""
import os
import sys
import tempfile
import random

os.environ["SGHY_PLAYER_DATA"] = os.path.join(
    tempfile.gettempdir(), "sghy_lottery_unit_test_player.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.game import lottery_config as lc
from sghy_server.proto import ps_protobuf_pb2 as pb


class FakeSession:
    def __init__(self):
        self.frames = []

    def send(self, opcode, body):
        self.frames.append((opcode, body))

    def log(self, *args):
        pass


def make_player(**kw):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 5000000, "diamond": 100000, "vigour": 1000,
                   "lv": 60, "vip": 0},
        "heroes": {},
        "equips": {},
        "items": dict(kw.pop("items", {})),
    })
    core.PLAYER_DATA["player"].update(kw)


def send(sess, opcode, msg=None):
    core.dispatch(sess, opcode, msg.SerializeToString() if msg is not None else b"")


def decode_all(sess, opcode, cls):
    out = []
    for op, body in sess.frames:
        if op == opcode:
            m = cls()
            m.ParseFromString(body)
            out.append(m)
    return out


def pool_types(pool_id):
    """池内所有 ObjectAward.Type（递归）。"""
    seen = set()

    def walk(pid):
        if pid in seen:
            return set()
        seen.add(pid)
        out = set()
        for _, refs in lc.groups(pid):
            for r in refs:
                if not isinstance(r, dict):
                    continue
                cn = r.get("ClassName")
                if cn == "QuoteAward":
                    out |= walk(r.get("AwardID"))
                elif cn == "ObjectAward":
                    out.add(int(r.get("Type") or 0))
        return out

    return walk(int(pool_id))


def recruit(sess, record_id, use_item=False):
    m = pb.UseLotteryData()
    m.id = record_id
    m.useItem = use_item
    send(sess, 5040, m)


# ------------------------------------------------------------------ tests

def test_records():
    make_player()
    sess = FakeSession()
    send(sess, 5041)
    datas = decode_all(sess, 5041, pb.LotteryRecordDatas)
    assert len(datas) == 1, "5041 应回一帧 LotteryRecordDatas"
    recs = list(datas[0].lotteryRecords)
    assert len(recs) == 10, "应有 10 条记录，实际 %d" % len(recs)
    types = sorted(r.type for r in recs)
    assert types == sorted(lc.LOTTERY_TYPES), types
    assert all(r.nextFreeTime == 0 for r in recs), "初始全部可免费"
    print("PASS 5041：10 条记录，类型齐全")


def test_first_free_101():
    make_player()
    sess = FakeSession()
    recruit(sess, 101)
    heroes = decode_all(sess, 476, pb.AllHeroMessage)
    assert len(heroes) == 1 and len(heroes[0].list) == 1, "101 应回 1 个武将"
    h = heroes[0].list[0]
    assert h.key == 404, "key 必须是 404(NewHero)，实际 %s" % h.key
    assert h.heroData.type in pool_types(2003), "首抽应来自 2003 池，实际 type=%s" % h.heroData.type
    assert int(core.game_config.get_hero_star_info(h.heroData.type)["quality"]) == 4
    rec = core._lottery_store()["101"]
    assert rec["first_done"] == 1, "首抽标记应已消耗"
    assert rec["total_times"] == 1
    assert rec["next_free"] > core.time.time() + 23 * 3600, "免费后应进入 24h 冷却"
    assert core.PLAYER_DATA["player"]["diamond"] == 100000, "免费抽不扣元宝"
    assert 200 in [op for op, _ in sess.frames], "应推 200 同步"
    print("PASS 101 首抽免费：2003 池 4★，进入 24h 冷却")


def test_first_ten_104():
    make_player()
    sess = FakeSession()
    recruit(sess, 104)
    heroes = decode_all(sess, 476, pb.AllHeroMessage)[0].list
    assert len(heroes) == 10, "十连应回 10 个武将，实际 %d" % len(heroes)
    last = heroes[-1].heroData
    assert last.type in lc.pool_hero_types(1004), "首次十连最后一抽应是 6★，实际 %s" % last.type
    assert int(core.game_config.get_hero_star_info(last.type)["quality"]) == 6
    assert core.PLAYER_DATA["player"]["diamond"] == 100000 - 2590
    print("PASS 104 首次十连：10 个武将，最后一抽 6★，扣 2590")


def test_pity_20():
    make_player()
    store = core._lottery_store()
    store["104"]["total_times"] = 1          # 10 抽
    store["101"]["total_times"] = 9          # 9 抽 -> 累计 19
    store["101"]["first_done"] = 1
    store["101"]["next_free"] = 0
    sess = FakeSession()
    recruit(sess, 101)                       # 第 20 抽
    heroes = decode_all(sess, 476, pb.AllHeroMessage)[0].list
    t = heroes[0].heroData.type
    five = lc.pool_hero_types(1030)
    assert t in five, "第 20 抽应走 1030 保底池（5★），实际 %s" % t
    assert int(core.game_config.get_hero_star_info(t)["quality"]) == 5
    print("PASS 20 抽保底：跨 101/104 共用计数，第 20 抽落在 1030")


def test_token_129():
    make_player(items={"20010": 10})
    sess = FakeSession()
    recruit(sess, 129, use_item=True)
    heroes = decode_all(sess, 476, pb.AllHeroMessage)[0].list
    assert len(heroes) == 10, "招贤令十连应回 10 个武将"
    assert core.PLAYER_DATA["items"]["20010"] == 0, "应扣 10 个招贤令"
    dels = decode_all(sess, 499, pb.DeleteData)
    assert any(d.typeID == 2 and d.id == 20010 for d in dels), \
        "令牌用完应推 499 DeleteProp(typeID=2)，否则客户端显示 0/1"
    assert core.PLAYER_DATA["player"]["diamond"] == 100000, "令牌路径不应扣元宝"
    print("PASS 129 招贤令十连：扣 10 令 + 451，产出 10 武将")


def test_first_free_102():
    make_player()
    sess = FakeSession()
    recruit(sess, 102)
    h = decode_all(sess, 476, pb.AllHeroMessage)[0].list[0].heroData
    hc = core.game_config.load_hero_config()[h.type]
    assert h.type == 10192 or int(hc.get("Quality")) == 2, "102 首抽应为张星彩 2★，实际 %s" % h.type
    assert core.PLAYER_DATA["player"]["vigour"] == 1000, "免费不扣元气"
    print("PASS 102 首抽免费：固定张星彩 2★")


def test_first_free_201():
    make_player()
    sess = FakeSession()
    recruit(sess, 201)
    specs = decode_all(sess, 5040, pb.AwardResult)
    assert len(specs) == 1 and len(specs[0].datas) == 1, "201 应回 1 条 AwardResult"
    assert specs[0].datas[0].type == 31111, "201 首抽应固定 31111，实际 %s" % specs[0].datas[0].type
    eqs403 = decode_all(sess, 403, pb.EquipData)
    assert len(eqs403) == 1 and eqs403[0].type == 31111, "应推 403 NewEquip"
    assert core.PLAYER_DATA["player"]["coin"] == 5000000, "免费不扣铜币"
    print("PASS 201 首抽免费：固定 31111 装备 + 403")


def test_first_ten_203():
    make_player(diamond=100000)
    sess = FakeSession()
    recruit(sess, 203)
    specs = decode_all(sess, 5040, pb.AwardResult)
    assert len(specs) == 1 and len(specs[0].datas) == 10, "203 应回 10 条奖励"
    purple = pool_types(3320)
    assert any(d.type in purple for d in specs[0].datas), "首次十连必含 3320 紫装"
    assert len(decode_all(sess, 403, pb.EquipData)) >= 1, "应有 403 装备推送"
    assert core.PLAYER_DATA["player"]["diamond"] == 100000 - 1780
    print("PASS 203 首次十连寻宝：10 条奖励含紫装，扣 1780")


def test_discount_104():
    make_player()
    core.gm_set_lottery_discount(50, 1)
    sess = FakeSession()
    recruit(sess, 104)
    assert core.PLAYER_DATA["player"]["diamond"] == 100000 - int(2590 * 50 / 100), \
        "折扣价应为 1295，实际扣 %d" % (100000 - core.PLAYER_DATA["player"]["diamond"])
    assert int(core.lottery_discount_info()["left"]) == 0, "折扣次数应 -1"
    print("PASS 104 折扣：2590×50%%=1295，次数 -1")


def test_reject():
    make_player(diamond=0)
    # 先消耗掉免费次数
    rec = core._lottery_store()["104"]
    rec["first_done"] = 1
    sess = FakeSession()
    recruit(sess, 104)
    ops = [op for op, _ in sess.frames]
    assert 476 not in ops, "失败不应发 476"
    assert 5002 in ops, "失败应发 5002"
    sm = decode_all(sess, 5002, pb.ShowMessage)[0]
    assert "资源不足" in sm.msg, sm.msg
    print("PASS 拒绝路径：5002 %r，无 476" % sm.msg)


if __name__ == "__main__":
    test_records()
    test_first_free_101()
    test_first_ten_104()
    test_pity_20()
    test_token_129()
    test_first_free_102()
    test_first_free_201()
    test_first_ten_203()
    test_discount_104()
    test_reject()
    print("TOTAL: 10 passed")
