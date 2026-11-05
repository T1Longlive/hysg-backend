"""图鉴 / 收集 单元测试（fake session）。

覆盖：
  1. 登录补种：当前武将/装备并入图鉴（老存档兼容）
  2. EnterGame 下发 heroRecords / equipRecords / collectionAwardRecords
  3. 进度口径：按 Origin / Part 统计已点亮的不同 type 数
  4. 进度不足 → 10115 ack（清 requesting）+ 5002，不发奖
  5. 领取武将里程碑 15 → Vigour +500、10115 记录 15、301 AwardResult
  6. 重复领取 → 拒绝（已领取）
  7. 领取装备里程碑 20 → Coin +300000
  8. 新点亮接口 _record_hero_collect 幂等
"""
import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(
    tempfile.gettempdir(), "sghy_collect_unit_test_player.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.game import collect_config
from sghy_server.proto import ps_protobuf_pb2 as pb


class FakeSession:
    def __init__(self):
        self.frames = []

    def send(self, opcode, body):
        self.frames.append((opcode, body))

    def log(self, *args):
        pass


def make_player():
    hmap = collect_config.hero_collect_map()
    emap = collect_config.equip_collect_map()
    origins = [t for t, m in hmap.items() if m["origin"] == 1][:15]
    parts = [t for t, m in emap.items() if m["part"] == 1][:20]
    heroes = {str(i + 1): {"id": i + 1, "type": t, "lv": 1, "exp": 0,
                           "grade": 0, "grade_exp": 0, "position": 0}
              for i, t in enumerate(origins)}
    equips = {str(i + 1): {"id": i + 1, "type": t, "host": 0, "strengthen": 0,
                           "grade": 0, "grade_exp": 0}
              for i, t in enumerate(parts)}
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"lv": 60, "coin": 1000, "diamond": 1000, "vigour": 100, "vip": 0},
        "heroes": heroes, "equips": equips, "items": {},
    })
    return origins, parts


def decode_all(sess, opcode, cls):
    out = []
    for op, body in sess.frames:
        if op == opcode:
            m = cls()
            m.ParseFromString(body)
            out.append(m)
    return out


def test_seed_and_enter_game():
    origins, parts = make_player()
    ht, et, aw = core.collect_snapshot()
    assert set(origins) <= set(ht), "登录补种应包含当前武将"
    assert set(parts) <= set(et), "登录补种应包含当前装备"
    eg = pb.EnterGame()
    eg.ParseFromString(core.build_enter_game())
    assert len(eg.heroRecords) >= 15, "EnterGame heroRecords=%d" % len(eg.heroRecords)
    assert len(eg.equipRecords) >= 20, "EnterGame equipRecords=%d" % len(eg.equipRecords)
    types = {r.type for r in eg.heroRecords}
    assert set(origins) <= types
    print("PASS 登录补种 + EnterGame：heroRecords=%d equipRecords=%d"
          % (len(eg.heroRecords), len(eg.equipRecords)))


def test_progress():
    make_player()
    assert core.collect_progress(1, 1) == 15, core.collect_progress(1, 1)
    assert core.collect_progress(2, 1) == 20, core.collect_progress(2, 1)
    assert core.collect_progress(1, 4) == 0
    print("PASS 进度口径：Origin1=15 / Part1=20 / Origin4=0")


def test_claim_locked():
    make_player()
    sess = FakeSession()
    m = pb.ReceiveCollectionAwardRequestData()
    m.type, m.subType, m.milestoneCount = 1, 1, 25   # 进度 15 < 25
    core.dispatch(sess, 10115, m.SerializeToString())
    ops = [op for op, _ in sess.frames]
    assert 10115 in ops, "失败也必须回 10115（清 requesting）"
    assert 5002 in ops, "失败应回 5002 说明"
    rec = decode_all(sess, 10115, pb.CollectionAwardRecordData)[0]
    assert list(rec.receivedMilestoneCounts) == [], "不得标记已领取"
    assert 301 not in ops, "失败不应弹奖励"
    print("PASS 进度不足：10115 + 5002，无奖励")


def test_claim_hero():
    make_player()
    sess = FakeSession()
    m = pb.ReceiveCollectionAwardRequestData()
    m.type, m.subType, m.milestoneCount = 1, 1, 15
    core.dispatch(sess, 10115, m.SerializeToString())
    rec = decode_all(sess, 10115, pb.CollectionAwardRecordData)[0]
    assert list(rec.receivedMilestoneCounts) == [15], list(rec.receivedMilestoneCounts)
    assert core.PLAYER_DATA["player"]["vigour"] == 100 + 500, core.PLAYER_DATA["player"]["vigour"]
    ar = decode_all(sess, 301, pb.AwardResult)[0]
    assert len(ar.datas) == 1 and ar.datas[0].type == 0
    assert ar.datas[0].name == "Vigour" and ar.datas[0].value == 500
    print("PASS 领取武将里程碑 15：Vigour+500 + 301 弹窗")


def test_claim_twice():
    make_player()
    for _ in range(2):
        sess = FakeSession()
        m = pb.ReceiveCollectionAwardRequestData()
        m.type, m.subType, m.milestoneCount = 1, 1, 15
        core.dispatch(sess, 10115, m.SerializeToString())
    ops = [op for op, _ in sess.frames]
    assert 10115 in ops and 5002 in ops and 301 not in ops, ops
    assert core.PLAYER_DATA["player"]["vigour"] == 100 + 500, "第二次不得重复发奖"
    print("PASS 重复领取：10115 + 5002，不重复发奖")


def test_claim_equip():
    make_player()
    sess = FakeSession()
    m = pb.ReceiveCollectionAwardRequestData()
    m.type, m.subType, m.milestoneCount = 2, 1, 20
    core.dispatch(sess, 10115, m.SerializeToString())
    rec = decode_all(sess, 10115, pb.CollectionAwardRecordData)[0]
    assert list(rec.receivedMilestoneCounts) == [20]
    assert core.PLAYER_DATA["player"]["coin"] == 1000 + 300000, core.PLAYER_DATA["player"]["coin"]
    print("PASS 领取装备里程碑 20：Coin+300000")


def test_record_idempotent():
    make_player()
    hmap = collect_config.hero_collect_map()
    # 找一个当前没点亮的 origin=2 武将
    candidate = next(t for t, m in hmap.items() if m["origin"] == 2 and t not in core._collect_store()["heroes"])
    first = core._record_hero_collect(candidate)
    second = core._record_hero_collect(candidate)
    assert first is True and second is False, (first, second)
    assert candidate in core.collect_snapshot()[0]
    print("PASS 点亮幂等：首次 True / 再次 False")


def test_honor_milestone():
    """武将 Origin1 的 55 里程碑奖励是头像框 55025：
    不能当道具入库（会污染 items），301 弹窗用 type=0 + 名称显示。"""
    make_player()
    c = core._collect_store()
    c["heroes"] = sorted(t for t, m in collect_config.hero_collect_map().items()
                         if m["origin"] == 1)
    sess = FakeSession()
    m = pb.ReceiveCollectionAwardRequestData()
    m.type, m.subType, m.milestoneCount = 1, 1, 55
    core.dispatch(sess, 10115, m.SerializeToString())
    ar = decode_all(sess, 301, pb.AwardResult)[0]
    name = core.game_config.get_honor_name(55025)
    assert ar.datas[0].type == 0 and ar.datas[0].name == name, (
        ar.datas[0].type, ar.datas[0].name)
    assert "55025" not in core.PLAYER_DATA["items"], "头像框不能当道具入库"
    print("PASS 头像框里程碑：301 用属性槽显示 %r，未污染 items" % name)


if __name__ == "__main__":
    test_seed_and_enter_game()
    test_progress()
    test_claim_locked()
    test_claim_hero()
    test_claim_twice()
    test_claim_equip()
    test_record_idempotent()
    test_honor_milestone()
    print("TOTAL: 8 passed")
