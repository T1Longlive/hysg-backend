"""装备升星（6023）dispatch 回归测试（2026-10-28）。

修的是一个真实报障：「升星成功后客户端还显示 4 星」。
根因：升星换的是**装备 type**（4星形态→5星形态），而客户端所有更新入口
（6023 ack / 452 / 467·472）都只走 Equip:updateWithSvrData —— 只更新 host/
强化/属性/阶，**从不更新 type/_star**（星级来自 EquipConfig，创建时定死）。
修法：升星成功后按 499(删旧实例) → 403(NewEquip 新 type) → 6023 ack 的顺序推送，
让客户端 addSvrEquip 按新 type 重建对象。

覆盖：
  · 成功：type 走 NextType 到下一星、阶清零、强化保留、扣铜币、材料被删
  · 包序：499(装备+2材料) 全部在 403 之前，403 在 6023 ack 之前
  · 拒绝：未满 9 阶 / 材料不同星 / 已满星 → 5002，且不发 403/6023

用法：python tools/equip_starup_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(
    tempfile.gettempdir(), "sghy_equip_starup_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sghy_server.game import core, growth
from sghy_server.proto import ps_protobuf_pb2 as pb

OP_STAR_UP = 6023
OP_DELETE_PROP = 499
OP_NEW_EQUIP = 403


class Sess:
    def __init__(self):
        self.sent = []

    def send(self, op, body):
        self.sent.append((op, body))

    def log(self, *a):
        pass


def _eq(eid, etype, grade=9, strengthen=120, host=0):
    return {"id": eid, "type": etype, "host": host, "strengthen": strengthen,
            "grade": grade, "grade_exp": 0}


def _reset(equips, coin=5_000_000):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": coin, "lv": 60, "vip": 8, "diamond": 0},
        "heroes": {}, "equips": equips, "items": {},
    })


def _dispatch(eq_id, mats):
    req = pb.IDListMessage()
    for i in [eq_id] + list(mats):
        req.id.append(int(i))
    s = Sess()
    core.dispatch(s, OP_STAR_UP, req.SerializeToString())
    return s


def _pick_four_star_type():
    """找一个 4 星、有 NextType（→5星）的装备 type。"""
    for etype in range(33000, 34200):
        row = growth.equip_row(etype)
        if not row:
            continue
        if growth.equip_star(etype) == 4 and growth.equip_next_type(etype):
            return etype
    raise AssertionError("配置里找不到 4 星且可升星的装备")


def test_star_up_ok():
    base = _pick_four_star_type()
    nxt = growth.equip_next_type(base)
    cost = growth.equip_star_up_cost(4)
    assert growth.equip_star(nxt) == 5, (base, nxt)

    _reset({**{str(i): _eq(i, base) for i in (900, 901, 902)}}, coin=cost + 1000)
    s = _dispatch(900, [901, 902])
    ops = [op for op, _ in s.sent]

    # ack 正确 + 形态切换
    assert OP_STAR_UP in ops, ops
    ack = pb.EquipData()
    ack.ParseFromString(dict(s.sent)[OP_STAR_UP])
    assert ack.id == 900 and ack.type == nxt, (ack.id, ack.type, nxt)
    assert ack.grade == 0, "升星后阶数必须清零"
    assert ack.StrengthenLv == 120, "强化等级必须保留"

    # 服务端状态
    eq = core.find_equip(900)
    assert eq["type"] == nxt and eq["grade"] == 0 and eq["strengthen"] == 120
    assert core.find_equip(901) is None and core.find_equip(902) is None, "材料必须被吃掉"
    assert core.PLAYER_DATA["player"]["coin"] == 1000, core.PLAYER_DATA["player"]["coin"]

    # ★ 包序回归（用户报障点）：499(装备+2材料) → 403(新 type) → 6023 ack
    assert 200 in ops, ops
    i_last_delete = max(i for i, op in enumerate(ops) if op == OP_DELETE_PROP)
    i_new = ops.index(OP_NEW_EQUIP)
    i_ack = ops.index(OP_STAR_UP)
    assert i_last_delete < i_new < i_ack, \
        "包序必须是 499 → 403 → 6023（否则客户端对象星级不刷新）: %s" % ops
    # 403 的 body 必须是新 type（客户端靠它重建 EquipData 对象）
    new_eq = pb.EquipData()
    new_eq.ParseFromString([b for op, b in s.sent if op == OP_NEW_EQUIP][0])
    assert new_eq.id == 900 and new_eq.type == nxt
    # 三个 499 都要有：目标装备 + 2 件材料
    dels = []
    for op, body in s.sent:
        if op == OP_DELETE_PROP:
            d = pb.DeleteData()
            d.ParseFromString(body)
            dels.append((d.typeID, d.id))
    assert sorted(i for _, i in dels) == [900, 901, 902], dels
    assert all(t == 3 for t, _ in dels), "DeleteData.typeID 必须=3（装备）: %s" % dels
    print("PASS 升星成功：type %s→%s 阶清零 强化保留 扣币%s 材料删除 / "
          "包序 499→403→6023 正确" % (base, nxt, cost))


def test_star_up_rejects():
    """拒绝路径：不给 403 重建、ack 里的 type 必须保持原值（ack 本身是无条件保活包）。"""
    base = _pick_four_star_type()

    def _expect_reject(equips, eid, mats):
        _reset(equips)
        s = _dispatch(eid, mats)
        ops = [op for op, _ in s.sent]
        assert 5002 in ops, "拒绝必须推 5002 说明原因: %s" % ops
        assert OP_NEW_EQUIP not in ops, "拒绝不能推 403（客户端会重建）: %s" % ops
        # ack 是无条件保活包（同 5008 教训：不回 ack 客户端锁输入），type 必须不变
        if OP_STAR_UP in ops:
            ack = pb.EquipData()
            ack.ParseFromString(dict(s.sent)[OP_STAR_UP])
            assert ack.type == equips[str(eid)]["type"], "被拒时 ack 不能带新 type"

    # ① 未满 9 阶
    _expect_reject({str(i): _eq(i, base, grade=3) for i in (910, 911, 912)}, 910, [911, 912])

    # ② 材料星级不符（拿一件 3 星同 Group 的）
    other = None
    for etype in range(33000, 34200):
        if not growth.equip_row(etype):
            continue
        if growth.equip_group(etype) == growth.equip_group(base) \
                and growth.equip_star(etype) == 3:
            other = etype
            break
    if other:
        _expect_reject({"910": _eq(910, base), "911": _eq(911, base),
                        "912": _eq(912, other)}, 910, [911, 912])

    # ③ 已满星（直接拿 5 星）
    five = growth.equip_next_type(base)
    _expect_reject({str(i): _eq(i, five) for i in (920, 921, 922)}, 920, [921, 922])
    print("PASS 升星拒绝：未满阶/材料不符/已满星 → 5002、不推 403、ack 不带新 type")


if __name__ == "__main__":
    growth.init()
    test_star_up_ok()
    test_star_up_rejects()
    print("\n全部通过 ✔")
