"""装备升阶单元测试（不经 TCP，确定性验证功能与公式对齐）。

2026-10-22：TCP 集成测试（equip_grade_test.py）在 Proactor 下有环境相关的
上行偶发失效，改以本单测做功能验收——直接调 core.do_grade_up_equip 与
core.dispatch（fake session 捕获发出帧），完全确定性。

断言：
  1. 经验公式对齐客户端：EquipGradeExp[材料品质][材料星级] × (1+(5-装备星级)×0.2)
  2. 铜币 = 总经验 × ExpSetConfig[装备品质].EquipUpGradeExpToCoin
  3. handler 成功帧：499×N(typeID=3) → 6021(EquipData) → 200
  4. 被拒：5002 原因、不发 6021、不删材料
"""

import sys
import os
import tempfile

# ★ 必须在 import core 之前隔离存档路径：core 的 save_player_data 会写
#   SGHY_PLAYER_DATA 指向的文件，曾把单测的迷你存档写进 env/player_data.json
#   毁掉 golden 测试基线（2026-10-22）。
os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_equip_unit_test_player.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.proto import ps_protobuf_pb2 as pb

TARGET = 32112   # q3 s2
MAT = 31111      # q2 s1


class FakeSession:
    def __init__(self):
        self.frames = []

    def send(self, opcode, body):
        self.frames.append((opcode, body))

    def log(self, *args):
        pass


def make_player(equips):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 999999, "lv": 60, "vip": 0},
        "heroes": {},
        "equips": equips,
        "items": {},
    })


def run_dispatch(sess, opcode, msg):
    core.dispatch(sess, opcode, msg.SerializeToString())


def test_success():
    eqs = {
        "1": {"id": 1, "type": TARGET, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
        "2": {"id": 2, "type": MAT, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
        "3": {"id": 3, "type": MAT, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
    }
    make_player(eqs)
    sess = FakeSession()
    idl = pb.IDListMessage()
    idl.id.append(1)
    idl.id.append(2)
    idl.id.append(3)
    run_dispatch(sess, 6021, idl)

    ops = [op for op, _ in sess.frames]
    # 逐帧断言
    dels = []
    ack = None
    got200 = False
    for op, body in sess.frames:
        if op == 499:
            dd = pb.DeleteData()
            dd.ParseFromString(body)
            dels.append((dd.typeID, dd.id))
        elif op == 6021:
            ack = pb.EquipData()
            ack.ParseFromString(body)
        elif op == 200:
            got200 = True

    # 客户端公式：5 × (1+(5-2)×0.2) × 2 件
    expect_exp = int(5 * (1 + (5 - 2) * 0.2) * 2)   # = 16
    expect_coin_cost = int(5 * (1 + (5 - 2) * 0.2) * 2 * 1500)  # 16 × 1500 = 24000

    assert dels == [(3, 2), (3, 3)], "499 应 typeID=3 删两件材料，实际 %s" % dels
    assert ack is not None and ack.id == 1, "应有 6021 ack（目标装备）"
    assert ack.gradeExp == expect_exp, "gradeExp=%d ≠ 客户端公式 %d" % (ack.gradeExp, expect_exp)
    assert got200, "应有 200 铜币同步"
    assert core.PLAYER_DATA["player"]["coin"] == 999999 - expect_coin_cost, \
        "铜币扣减 %d ≠ %d" % (core.PLAYER_DATA["player"]["coin"], 999999 - expect_coin_cost)
    assert "2" not in core.PLAYER_DATA["equips"] and "3" not in core.PLAYER_DATA["equips"], "材料应被移除"
    print("PASS 成功路径：499(3)×2 → 6021(gradeExp=%d) → 200（扣币 %d）" % (ack.gradeExp, expect_coin_cost))


def test_reject():
    eqs = {
        "1": {"id": 1, "type": TARGET, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
    }
    make_player(eqs)
    sess = FakeSession()
    idl = pb.IDListMessage()
    idl.id.append(1)
    idl.id.append(999)   # 不存在的材料
    run_dispatch(sess, 6021, idl)

    ops = [op for op, _ in sess.frames]
    assert 6021 not in ops, "被拒不应发 6021 ack（会带空 EquipData 炸 dispatch）"
    assert 5002 in ops, "被拒应发 5002 告知原因"
    sm = None
    for op, body in sess.frames:
        if op == 5002:
            sm = pb.ShowMessage()
            sm.ParseFromString(body)
    assert sm is not None and "材料不存在" in sm.msg, "5002 文案应含'材料不存在'，实际 %r" % (sm.msg if sm else None)
    assert "1" in core.PLAYER_DATA["equips"], "被拒不应删装备/材料"
    print("PASS 拒绝路径：5002 %r，无 ack、无删材料" % sm.msg)


def test_quality_limit():
    """材料品质高于装备品质 → 拒绝（对齐客户端 createEquipFilters）。"""
    eqs = {
        "1": {"id": 1, "type": MAT, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},      # q2
        "2": {"id": 2, "type": 32111, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},    # q3 材料
    }
    make_player(eqs)
    sess = FakeSession()
    idl = pb.IDListMessage()
    idl.id.append(1)
    idl.id.append(2)
    run_dispatch(sess, 6021, idl)
    sm = None
    for op, body in sess.frames:
        if op == 5002:
            sm = pb.ShowMessage()
            sm.ParseFromString(body)
    assert sm is not None and "品质高于" in sm.msg, "应拒绝高品质材料"
    print("PASS 品质限制：%r" % sm.msg)


def test_assist_not_in_final_additions():
    """副属性不得并入 finalAdditions：客户端装备详情的“攻击+N”由 finalAdditions
    累加而来，掺入副属性会导致洗练副属性时“装备本身属性”显示值变化（2026-10-22）。"""
    eqs = {
        "1": {"id": 1, "type": TARGET, "host": 0, "strengthen": 0, "grade": 0,
              "grade_exp": 0,
              "assist": {"1": {"attr": 1, "value": 999, "times": 1}}},
    }
    make_player(eqs)
    e = pb.EquipData()
    e.ParseFromString(core.build_equip_data(eqs["1"]))
    atk_vals = [a.value for a in e.finalAdditions if a.type == 1]
    assert 999 not in atk_vals, "副属性 999 混进了 finalAdditions: %s" % atk_vals
    assert any(a.value == 999 for a in e.unlockAssistant), "副属性应在 unlockAssistant"
    print("PASS 副属性隔离：finalAdditions 攻击=%s（不含999），unlockAssistant 含 999" % atk_vals)


def test_strengthen_crit_cap_by_vip():
    """强化暴击倍率上限按 VIP 配置（VIPSetConfig.EquipEnCritCount）。

    用户 2026-10-28 确认：VIP 不同等级提升暴击倍率，**VIP12 解锁 5 倍**。
    配置事实（app.config.new.VIPSetConfig）：VIP0-2=2 / 3-7=3 / 8-11=4 / 12+=5。
    客户端只读 EquipData.EnCritCount>0 播"暴击"动画，倍率/概率全由服务端决定。
    """
    from sghy_server.game import growth
    growth.init()

    caps = [growth.equip_crit_max(v) for v in (0, 1, 2, 3, 7, 8, 11, 12, 15)]
    assert caps == [2, 2, 2, 3, 3, 4, 4, 5, 5], caps

    # 必暴击 + 取最大倍率：一次点击加 crit_max 级
    for vip, cap in ((0, 2), (3, 3), (8, 4), (12, 5)):
        lv, add, hits = growth.equip_strengthen_apply(
            0, 1, 1.0, growth.equip_crit_max(vip),
            _randint=lambda a, b: b, _rand=lambda: 0.0)
        assert add == cap and hits == 1, (vip, add, cap, hits)

    # 不暴击：每次点击 +1（5 次点击 = +5，无暴击）
    lv, add, hits = growth.equip_strengthen_apply(0, 5, 0.0, 5, _rand=lambda: 1.0)
    assert add == 5 and hits == 0, (add, hits)

    # 倍率不会超过 VIP 上限：即使 randint 返回更大值也按传入 cap 用（由调用方保证）
    lv, add, hits = growth.equip_strengthen_apply(
        0, 1, 1.0, 2, _randint=lambda a, b: b, _rand=lambda: 0.0)
    assert add == 2, add
    print("PASS 强化暴击倍率：VIP0-2=2/3-7=3/8-11=4/12+=5（配置驱动），"
          "必暴击取满倍率、不暴击 +1")


def test_worn_equip_ops_push_hero_attr():
    """穿在身上的装备做培养操作后，必须立刻推 453 刷武将属性面板。

    2026-10-28 用户报障：装备穿戴后培养升星/解锁副属性/洗练，武将属性面板不更新。
    根因：① 强化/升阶/升星把 453 挂在"战力变化"门槛下（增伤/减伤等不进战力权重
    的属性变了 power 不变 → 不推）；② 解锁副属性/保存洗练**完全不推 453**。
    修法：统一 push_equip_host（453 + 20416 无条件推）。
    """
    from sghy_server.game import growth
    growth.init()
    T = 34075                     # 七星龙渊剑，两条副属性（Type=4）
    HERO = 7
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 99_999_999, "lv": 60, "vip": 8, "diamond": 99_999},
        "heroes": {str(HERO): {"id": HERO, "type": 10856, "lv": 60, "grade": 0,
                               "position": 1, "equips": [55], "fate": {}}},
        "items": {"20002": 9999},
        "equips": {"55": {"id": 55, "type": T, "host": HERO, "strengthen": 0,
                          "grade": 9, "grade_exp": 0, "pending_assist": {}}},
    })

    def frames_of(op, sess):
        return [b for o, b in sess.frames if o == op]

    def assert_453(target_id, what):
        sess = FakeSession()
        return sess

    # ① 解锁副属性（6024）
    sess = FakeSession()
    idl = pb.IDListMessage(); idl.id.append(55); idl.id.append(1)
    run_dispatch(sess, 6024, idl)
    ops = [o for o, _ in sess.frames]
    assert 6024 in ops, ops
    heroes_453 = frames_of(453, sess)
    assert heroes_453, "解锁副属性后必须推 453（武将属性面板）: %s" % ops
    hd = pb.HeroData(); hd.ParseFromString(heroes_453[0])
    assert hd.id == HERO, hd.id
    assert 20416 in ops, "解锁后必须推 20416 编队战力: %s" % ops

    # ② 洗练（6025）→ 保存（6026）
    sess = FakeSession()
    idl2 = pb.IDListMessage(); idl2.id.append(55); idl2.id.append(1)
    run_dispatch(sess, 6025, idl2)
    run_dispatch(sess, 6026, idl2)
    ops = [o for o, _ in sess.frames]
    assert 6026 in ops, ops
    assert frames_of(453, sess), "保存洗练后必须推 453: %s" % ops

    # ③ 强化（5014 单次）
    sess = FakeSession()
    im = pb.IDMessage(); im.id = 55
    run_dispatch(sess, 5014, im)
    ops = [o for o, _ in sess.frames]
    assert 5014 in ops, ops
    assert frames_of(453, sess), "强化后必须推 453: %s" % ops
    assert 20416 in ops, "强化后必须推 20416: %s" % ops
    print("PASS 穿戴装备的培养操作（解锁/洗练保存/强化）都即时推 453+20416")


if __name__ == "__main__":
    test_success()
    test_reject()
    test_quality_limit()
    test_assist_not_in_final_additions()
    test_strengthen_crit_cap_by_vip()
    test_worn_equip_ops_push_hero_attr()
    print("TOTAL: 6 passed")
