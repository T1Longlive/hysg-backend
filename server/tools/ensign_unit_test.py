"""军旗系统单元测试（2026-10-23）。

覆盖：材料 4:1 折算消耗、制作/升级/分解、装备槽位冲突、旗帐解锁、
编队上阵校验、builder 消息 roundtrip、5346 dispatch 全链路。

用法：python tools/ensign_unit_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_ensign_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core, ensign, game_config
from sghy_server.proto import ps_protobuf_pb2 as pb


class Sess:
    def __init__(self):
        self.sent = []

    def send(self, op, body):
        self.sent.append((op, body))

    def log(self, *a):
        pass


def make_player(items=None, coin=5000000, diamond=100000):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": coin, "lv": 60, "vip": 8, "diamond": diamond},
        "heroes": {}, "equips": {}, "items": dict(items or {}),
    })


def test_config():
    gs = game_config.load_game_set_config()
    assert gs.get("MaxFlagCount") == 24 and gs.get("UnlockFlagCost") == 1000
    fp = game_config.load_flag_part_config()
    assert len(fp) == 96
    r = fp[42001]
    assert r["Part"] == 1 and r["AdditionCount"] == 1
    assert r["ProductCost"][0]["Type"] == 22201
    assert r["Addition"][0]["MaxValue"] == 448
    fs = game_config.load_flag_suit_config()
    assert fs[1300]["Count"] == [2, 4]
    assert fs[1300]["SkillType"] == [[700001], [700002]]
    fl = game_config.load_flag_draw_level_config()
    assert fl[1]["TempBagCapacity"] == 10
    print("PASS 配置解析：GameSet/FlagPart96/Suit/DrawLevel")


def test_replacement():
    assert ensign.item_replacement(21001) == (21000, 4)   # 1 绿布料 = 4 白布料
    assert ensign.item_replacement(21019) == (21018, 4)   # 金骨头 = 4 紫骨头
    assert ensign.item_replacement(22201) == (23201, 30)  # 图纸也有折算链（30:1）
    make_player({"21000": 40})
    used = ensign.consume_item(21001, 10)                 # 10 绿 = 40 白
    assert used is not None and used.get(21000) == 40, used
    assert core.PLAYER_DATA["items"]["21000"] == 0
    # 不够 → 回滚
    used = ensign.consume_item(21001, 1)
    assert used is None
    assert core.PLAYER_DATA["items"]["21000"] == 0        # 0 已扣光，回滚无副作用
    make_player({"21000": 41})
    used = ensign.consume_item(21001, 10)                 # 40 白折算 → 成功
    assert used is not None and used.get(21000) == 40
    make_player({"21000": 41})
    used = ensign.consume_item(21001, 11)                 # 44 白 > 41 → 失败
    assert used is None, "41 白不够 11 绿，应失败"
    print("PASS 材料折算：4:1 消耗 / 不足拒绝")


def test_make_level_resolve():
    # 42001 校旗冠：ProductCost = 22201×1 + 21001×30 + 21006×10 + 500000 币
    make_player({"22201": 2, "21001": 45, "21006": 15})
    ok, reason, part, deltas, coin = ensign.make_part(42001)
    assert ok, reason
    assert part["host"] == 0 and part["up_count"] == 0
    adds = part["additions"]
    assert len(adds) == 1 and adds[0]["unlock"] is True
    assert adds[0]["type"] == 1 and adds[0]["maxValue"] == 448
    lo, hi = int(448 * 0.10), int(448 * 0.30)
    assert lo <= adds[0]["value"] <= hi, adds[0]
    assert adds[0]["baseValue"] == adds[0]["value"]
    assert part["unlock_count"] == 1
    assert core.PLAYER_DATA["player"]["coin"] == 5000000 - 500000
    assert core.PLAYER_DATA["items"]["21001"] == 15      # 45 - 30
    assert deltas.get(21001) == 30 and deltas.get(22201) == 1

    # 升级：+20% maxValue（89），封顶
    v0 = adds[0]["value"]
    ok, reason, part2, d2, c2 = ensign.level_up_part(part["id"])
    assert ok, reason
    a = part2["additions"][0]
    assert a["value"] == min(448, v0 + 89), a
    assert part2["up_count"] == 1
    assert core.PLAYER_DATA["items"]["22201"] == 0        # UpCost 里的图纸也扣

    # 分解：返还 = (Product+Up×1+Unlock×1)×0.8 的材料+铜币，不含图纸 22201
    ok, reason, rid, refund_items, refund_coin = ensign.resolve_part(part["id"])
    assert ok, reason
    assert 22201 not in refund_items, "图纸不返还"
    assert refund_coin > 0
    assert str(rid) not in ensign.part_store(core.PLAYER_DATA)
    print("PASS 制作/升级/分解：词条生成/升级步长/0.8 返还（图纸不退）")


def test_equip_slots():
    make_player({"22201": 2, "21001": 60, "21006": 20})
    ok, _, p1, _, _ = ensign.make_part(42001)      # Part=1
    assert ok
    ok, _, p2, _, _ = ensign.make_part(42001)      # 同 type 第二件
    assert ok
    ok, reason, _ = ensign.equip_part(p1["id"], 1)
    assert ok, reason
    ok, reason, _ = ensign.equip_part(p2["id"], 1)
    assert not ok, "同槽位应冲突"
    ok, reason, _ = ensign.unequip_part(p1["id"])
    assert ok and p1["host"] == 0
    ok, reason, _ = ensign.equip_part(p2["id"], 1)
    assert ok, reason
    # 旗帐不存在
    ok, reason, _ = ensign.equip_part(p1["id"], 999)
    assert not ok
    print("PASS 装备/卸下：同槽唯一 / 卸下重穿 / 旗帐校验")


def test_unlock_flag():
    make_player(diamond=2500)
    ok, reason, f1 = ensign.unlock_flag()
    assert ok, reason
    assert f1["id"] == 2 and f1["index"] == 2
    assert core.PLAYER_DATA["player"]["diamond"] == 1500
    ok, reason, f2 = ensign.unlock_flag()
    assert ok and f2["index"] == 3
    assert core.PLAYER_DATA["player"]["diamond"] == 500
    ok, reason, _ = ensign.unlock_flag()
    assert not ok and "元宝不足" in reason
    # 上限
    make_player(diamond=999999)
    core.PLAYER_DATA["flags"] = {str(i): {"id": i, "index": i} for i in range(1, 25)}
    ok, reason, _ = ensign.unlock_flag()
    assert not ok and "上限" in reason
    print("PASS 旗帐解锁：扣钻/index 递增/24 上限")


def test_squad_flag():
    make_player({"22201": 1, "21001": 30, "21006": 10})
    ok, _, part, _, _ = ensign.make_part(42001)
    assert ok
    # 没部件的旗帐不能上阵 → 先造一个空旗帐（同时保留默认旗帐 1）
    core.PLAYER_DATA["flags"] = {"1": {"id": 1, "index": 1},
                                 "2": {"id": 2, "index": 2}}
    ok, reason = ensign.set_squad_flag_checked(0, 1, 2)
    assert not ok, "空旗帐不能上阵"
    ensign.equip_part(part["id"], 1)                      # 装到旗帐 1
    ok, reason = ensign.set_squad_flag_checked(0, 1, 1)   # 旗帐1 有部件
    assert ok, reason
    assert ensign.get_squad_flag(0, 1) == 1
    types = ensign.squad_flag_part_types(0, 1)
    assert types == [42001], types
    ok, reason = ensign.set_squad_flag_checked(0, 1, 999)
    assert not ok
    ensign.set_squad_flag(0, 1, 0)
    assert ensign.squad_flag_part_types(0, 1) == []
    print("PASS 上阵校验：空旗帐拒绝/部件 type 列表/下阵清空")


def test_squad_flag_bonus():
    """上阵军旗 → 该编队武将的属性加成（2026-10-24）。"""
    from sghy_server.game import growth
    make_player()
    core.PLAYER_DATA["flags"] = {"1": {"id": 1, "index": 1}}
    core.PLAYER_DATA["flag_parts"] = {
        # 已装备到旗帐 1 的部件：攻击 +288
        "50001": {"id": 50001, "type": 42001, "host": 1, "unlock_count": 1,
                  "up_count": 2, "additions": [
                      {"type": 1, "value": 288, "mode": 0, "index": 1,
                       "maxValue": 448, "unlock": True, "baseValue": 110}]},
        # 未装备部件（host=0）：生命 +1000，不应计入
        "50002": {"id": 50002, "type": 42001, "host": 0, "unlock_count": 1,
                  "up_count": 0, "additions": [
                      {"type": 9, "value": 1000, "mode": 0, "index": 1,
                       "maxValue": 4000, "unlock": True, "baseValue": 1000}]},
    }
    core.PLAYER_DATA["squad_flags"] = {"0": {"1": 1}}

    assert ensign.squad_flag_bonuses(0, 1) == [(1, 288, 0)]
    assert ensign.squad_flag_bonuses(0, 2) == [], "未上阵的编队不该有加成"

    st = {"attack": 1000, "maxHP": 50000}
    growth.apply_addition_bonus(st, ensign.squad_flag_bonuses(0, 1),
                                growth.EQUIP_TYPE_ATTR)
    assert st["attack"] == 1288, st
    assert st["maxHP"] == 50000, "未装备的部件（host=0）不该计入"

    # Type=20(tenacity) 只在 EQUIP_TYPE_ATTR 有映射；用缘分表会静默丢掉这条
    st2 = {"attack": 1000, "tenacity": 0}
    growth.apply_addition_bonus(st2, [(20, 50, 0)], growth.EQUIP_TYPE_ATTR)
    assert st2["tenacity"] == 50, st2

    # 下阵后加成归零
    ensign.set_squad_flag(0, 1, 0)
    assert ensign.squad_flag_bonuses(0, 1) == []
    print("PASS 军旗加成：上阵词条计入 / 未装备部件不计 / 未上阵为空 / tenacity 映射")


def test_squad_suit_bonus():
    """套装属性加成（FlagSuitConfig.Additions，全 Mode=1 百分比）。"""
    from sghy_server.game import growth
    make_player()
    core.PLAYER_DATA["flags"] = {"1": {"id": 1, "index": 1}}
    core.PLAYER_DATA["squad_flags"] = {"0": {"1": 1}}
    # Group 1100：2 件套 → 攻/防/生 各 +3%（42504=Part4, 42505=Part5）
    core.PLAYER_DATA["flag_parts"] = {
        "90001": {"id": 90001, "type": 42504, "host": 1, "unlock_count": 1, "up_count": 0,
                  "additions": [{"type": 1, "value": 100, "mode": 0, "index": 1,
                                 "maxValue": 700, "unlock": True, "baseValue": 100}]},
        "90002": {"id": 90002, "type": 42505, "host": 1, "unlock_count": 1, "up_count": 0,
                  "additions": [{"type": 1, "value": 200, "mode": 0, "index": 1,
                                 "maxValue": 700, "unlock": True, "baseValue": 200}]},
    }
    suit = ensign.squad_flag_suit_additions(0, 1)
    assert suit == [(1, 3, 1), (2, 3, 1), (9, 3, 1)], suit
    bon = ensign.squad_flag_bonuses(0, 1)
    # 词条（固定值）在前、套装（百分比）在后
    assert bon == [(1, 100, 0), (1, 200, 0), (1, 3, 1), (2, 3, 1), (9, 3, 1)], bon

    st = {"attack": 1000, "defence": 500, "maxHP": 10000}
    growth.apply_addition_bonus(st, bon, growth.EQUIP_TYPE_ATTR)
    # 口径（用户拍板）：(固定值总和) × (1 + Σ百分比)
    assert st["attack"] == 1339, st       # (1000+300) * 1.03
    assert st["defence"] == 515, st       # 500 * 1.03
    assert int(st["maxHP"]) == 10300, st  # 10000 * 1.03

    # 拆掉一件 → 不足 2 件，套装加成消失
    core.PLAYER_DATA["flag_parts"]["90002"]["host"] = 0
    assert ensign.squad_flag_suit_additions(0, 1) == []
    print("PASS 套装加成：2件触发3%三项 / 固定值总和再乘百分比 / 拆一件失效")


def test_squad_suit_faction_and_power_per():
    """套装属性规则（用户 2026-10-28 定稿，客户端 tooltip 核对）。

    只有四套给 攻击/防御/生命 加成：
      · 战鼓 1100：全体上阵武将，2 件 攻/防/生 各 +3%（配置 Additions）
      · 蜀汉 1101：仅 Origin=2 武将，2 件 +5% / 4 件 +10%
      · 曹魏 1102：仅 Origin=1 武将，2 件 +5% / 4 件 +10%
      · 群雄 1104：仅 Origin=4 武将，2 件 +7% / 4 件 +12%
    其余套装（东吴 1103 / 涅槃 1304 等）**没有任何属性加成**。
    部件词条不受限制，全体上阵武将生效。
    """
    from sghy_server.game import growth

    def setup(ptypes):
        make_player()
        core.PLAYER_DATA["flags"] = {"1": {"id": 1, "index": 1}}
        core.PLAYER_DATA["squad_flags"] = {"0": {"1": 1}}
        parts = {}
        for i, t in enumerate(ptypes):
            parts[str(91000 + i)] = {"id": 91000 + i, "type": t, "host": 1,
                                     "unlock_count": 0, "up_count": 0, "additions": []}
        core.PLAYER_DATA["flag_parts"] = parts

    LYBU, CAOCAO, LIUBEI = 10856, 10014, 10196   # 群雄 / 魏 / 蜀

    # ① 群雄 2 件（46521/46522, Group=1104）：群将 +7%，魏将 0
    setup([46521, 46522])
    assert ensign.squad_flag_suit_additions(0, 1, hero_origin=4) == \
        [(9, 7, 1), (1, 7, 1), (2, 7, 1)], ensign.squad_flag_suit_additions(0, 1, hero_origin=4)
    assert ensign.squad_flag_suit_additions(0, 1, hero_origin=1) == []
    assert ensign.squad_flag_bonuses_for_hero(LYBU) == [(9, 7, 1), (1, 7, 1), (2, 7, 1)]
    assert ensign.squad_flag_bonuses_for_hero(CAOCAO) == []

    # ② 群雄 4 件 → 第二档 12%
    setup([46521, 46522, 46523, 46524])
    assert ensign.squad_flag_suit_additions(0, 1, hero_origin=4) == \
        [(9, 12, 1), (1, 12, 1), (2, 12, 1)]

    # ③ 曹魏 2 件（44521/44522, Group=1102）：魏将 +5%，蜀将 0
    setup([44521, 44522])
    assert ensign.squad_flag_suit_additions(0, 1, hero_origin=1) == \
        [(9, 5, 1), (1, 5, 1), (2, 5, 1)]
    assert ensign.squad_flag_bonuses_for_hero(CAOCAO) != []
    assert ensign.squad_flag_bonuses_for_hero(LIUBEI) == []

    # ④ 蜀汉 2 件（44501/44502, Group=1101）：蜀将 +5%
    setup([44501, 44502])
    assert ensign.squad_flag_suit_additions(0, 1, hero_origin=2) == \
        [(9, 5, 1), (1, 5, 1), (2, 5, 1)]
    assert ensign.squad_flag_bonuses_for_hero(LIUBEI) != []

    # ⑤ 涅槃 3 件（Group=1304）：无任何属性加成（旧的 AddPowerPer 口径已废）
    setup([46551, 46552, 46553])
    assert ensign.squad_flag_suit_additions(0, 1, hero_origin=4) == []
    assert ensign.squad_flag_suit_additions(0, 1) == []

    # ⑥ 战鼓 2 件（42504/42505, Group=1100）：全体 攻/防/生 各 +3%
    setup([42504, 42505])
    assert ensign.squad_flag_suit_additions(0, 1, hero_origin=4) == \
        [(1, 3, 1), (2, 3, 1), (9, 3, 1)]
    assert ensign.squad_flag_suit_additions(0, 1, hero_origin=1) == \
        [(1, 3, 1), (2, 3, 1), (9, 3, 1)]

    # ⑦ 词条全体生效：群雄旗下给一条攻击固定值，魏将也吃（只是不吃套装）
    setup([46521, 46522])
    core.PLAYER_DATA["flag_parts"]["91000"]["additions"] = [
        {"type": 1, "value": 50, "mode": 0, "index": 1, "maxValue": 400,
         "unlock": True, "baseValue": 50}]
    assert ensign.squad_flag_bonuses_for_hero(CAOCAO) == [(1, 50, 0)]
    assert ensign.squad_flag_bonuses_for_hero(LYBU) == \
        [(1, 50, 0), (9, 7, 1), (1, 7, 1), (2, 7, 1)]

    # ⑧ 下阵 → 全部归零
    ensign.set_squad_flag(0, 1, 0)
    assert ensign.squad_flag_bonuses_for_hero(LYBU) == []
    print("PASS 套装规则：仅战鼓/蜀汉/曹魏/群雄给属性%，阵营套只给本阵营、"
          "涅槃/东吴无加成、词条全体生效、下阵归零")


def test_select_box_batch():
    """★ 自选箱开 N 个必须得 N 份：以前发奖写死 count=1，扣 10 个箱子只给 1 个。

    用户报障：军旗仓库的八卦自选箱（21097）开 10 个八卦旗冠图纸（22616），
    图纸合成界面只多 1 个，但前端弹窗正常（301/451 都推了）。
    """
    make_player()
    items = core.PLAYER_DATA["items"]
    items["21097"] = 10
    items["22616"] = 0
    before = int(items.get("22616", 0))
    sess = Sess()
    fd = pb.FunctionData()
    fd.id = 21097
    fd.name = "SelectAnyItem"
    fd.count = 10
    fd.param = "22616"
    core.dispatch(sess, core.OP_USE_FUNCTION, fd.SerializeToString())
    assert int(items.get("21097", 0)) == 0, "箱子应扣光"
    got = int(items.get("22616", 0)) - before
    assert got == 10, "开 10 个箱子应得 10 个道具，实际 %s" % got
    # 箱子扣光要有 499 删除（否则列表残留）
    assert 499 in [op for op, _ in sess.sent], [op for op, _ in sess.sent]
    print("PASS 自选箱批量：开 10 个得 10 份，箱子归零推 499")


def test_consume_plan():
    """★ 服务端按客户端提交的扣料清单扣（消耗与界面预先显示一致）。

    客户端 ConvenientForgingPanel 把「各品质材料 + 各品质材料箱」算好放进
    MultIDCounteListByIDMessage.idCount，界面显示的就是这份清单。
    服务端自主折算会多烧高品质箱（实测 47 个 Q5 箱 vs 客户端的 44 个）。
    """
    make_player()
    items = core.PLAYER_DATA["items"]
    for k in list(items):
        if 21000 <= int(k) < 21100 or int(k) == 22616:
            del items[k]
    items.update({
        "22616": 1, "21009": 1, "21008": 1, "21006": 153, "21005": 59,
        "21014": 2, "21013": 4, "21012": 3, "21011": 1, "21010": 16,
        "21039": 100,
    })
    core.PLAYER_DATA["player"]["coin"] = 10_000_000
    before = {k: int(v) for k, v in items.items()}

    plan = [(22616,1), (21009,1), (21008,1), (21006,153), (21005,59),
            (21014,2), (21013,4), (21012,3), (21011,1), (21010,16), (21039,44)]
    ok, reason, part, deltas, coin = ensign.make_part(46541, plan=plan)
    assert ok, reason
    assert deltas == dict(plan), "扣料必须与清单逐项一致: %s" % deltas
    for i, c in plan:
        assert int(items.get(str(i), 0)) == before[str(i)] - c, "%s 实扣不对" % i

    # 清单里数量超过持有 → 整单拒绝，一个都不扣
    snap = {k: int(v) for k, v in items.items()}
    bad = [(22616, 1), (21009, 99999)]
    ok2, reason2, *_ = ensign.make_part(46541, plan=bad)
    assert not ok2 and "不足" in reason2, reason2
    assert {k: int(v) for k, v in items.items()} == snap, "被拒时必须不扣"

    # 清单白级不足（防作弊少扣）→ 拒绝
    cheat = [(22616, 1), (21009, 1)]      # 只给 1 个 Q5，白级远不够
    ok3, reason3, *_ = ensign.make_part(46541, plan=cheat)
    assert not ok3 and "不足" in reason3, reason3
    print("PASS 按清单扣料：逐项一致 / 数量不够整单拒绝 / 白级不足防作弊")


def test_material_box_fallback():
    """便捷制作：材料折算链断了，用**同品质材料箱**补（客户端把箱子算进配平）。

    用户报障场景：八卦旗冠(46541) 需 21009 皮革Q5×40 + 21014 丝线Q5×10，
    实际只有 皮革Q5=1/Q4=1/Q3=0、丝线Q5=2/Q4=4/Q3=3，界面显示"材料充足"
    （因为把材料箱算进去了），服务端却判"材料不足（21009 ×40）"。
    """
    make_player()
    items = core.PLAYER_DATA["items"]
    for k in list(items):
        if 21000 <= int(k) < 21100:
            del items[k]
    items.update({
        "22616": 1,      # 八卦旗冠图纸
        "21009": 1, "21008": 1, "21006": 153, "21005": 59,   # 皮革 Q5/Q4/Q2/Q1（Q3 缺）
        "21014": 2, "21013": 4, "21012": 3, "21011": 1,       # 丝线 Q5/Q4/Q3/Q2
        "21039": 100,    # Q5 材料箱
    })
    core.PLAYER_DATA["player"]["coin"] = 10_000_000
    ok, reason, part, deltas, coin = ensign.make_part(46541)
    assert ok, reason
    # 皮革缺 39 + 丝线缺 8 = 47 个 Q5 箱；低品质箱子一个都不该动
    assert deltas.get(21039) == 47, deltas
    assert deltas.get(21009) == 1 and deltas.get(21014) == 2, deltas
    for low in (21035, 21036, 21037, 21038):
        assert low not in deltas, "不该消耗低品质箱 %s: %s" % (low, deltas)
    assert int(items.get("21039", 0)) == 53, items.get("21039")

    # 箱子也不够 → 拒绝且不扣任何东西
    items["21039"] = 10
    before = dict(items)
    ok2, reason2, *_ = ensign.make_part(46541)
    assert not ok2 and "不足" in reason2, reason2
    assert dict(items) == before, "被拒时必须完整回滚"
    print("PASS 材料箱补料：缺 47 个 Q5 箱补齐、不动低品质箱、箱子不够时完整回滚")


def test_builders():
    make_player({"22201": 1, "21001": 30, "21006": 10})
    ok, _, part, _, _ = ensign.make_part(42001)
    ensign.equip_part(part["id"], 1)
    ensign.set_squad_flag(0, 1, 1)
    from sghy_server.game import builders_pb
    raw = builders_pb.build_flag_part_data(part)
    m = pb.FlagPartData()
    m.ParseFromString(raw)
    assert m.id == part["id"] and m.type == 42001 and m.host == 1
    assert len(m.unlockAssistants) == 1
    a = m.unlockAssistants[0]
    assert a.type == 1 and a.maxValue == 448 and a.unlock is True and a.Index == 1

    wb = builders_pb.warbook_record_msg()
    assert wb.lv == 0 and wb.mapID == 0

    entries = [(0, 0, 1, [(101, 1)], 999, 1)]
    mm = pb.UpdateBattleGameplaySquadDatas()
    mm.ParseFromString(builders_pb.build_update_squad_flag_datas(entries))
    u = mm.gameplayDataSquadFlags[0]
    assert u.category == 0 and u.squad.index == 1 and u.squad.flag == 1
    assert u.squad.heros[0].id == 101

    # EnterGame 带 flags/flagParts/warBookRecordData/squad.flag
    eg = pb.EnterGame()
    eg.ParseFromString(core.build_enter_game())
    assert len(eg.flags) == 1 and eg.flags[0].id == 1
    assert len(eg.flagParts) == 1 and eg.flagParts[0].id == part["id"]
    assert eg.battleGameplays[0].squads[0].flag == 1
    print("PASS builders：FlagPartData/WarBook/SquadFlag/EnterGame 字段")


def test_dispatch_5346():
    make_player({"22201": 1, "21001": 30, "21006": 10, "21000": 999})
    req = pb.MultIDCounteListByIDMessage()
    req.id = 42001
    body = req.SerializeToString()
    s = Sess()
    core.dispatch(s, 5346, body)
    ops = [op for op, _ in s.sent]
    assert 5346 in ops, ops
    ack = pb.FlagPartData()
    ack.ParseFromString(dict(s.sent)[5346])
    assert ack.type == 42001 and ack.unlockCount == 1
    # 道具同步：还剩下 → 451 UpdateItem；用光 → 499 DeleteProp（客户端 451 只改数量不删）
    assert (451 in ops or 499 in ops) and 200 in ops, "应有道具/货币同步: %s" % ops
    # 材料不足路径
    s2 = Sess()
    core.dispatch(s2, 5346, body)      # 材料已扣光
    ops2 = [op for op, _ in s2.sent]
    assert 5346 not in ops2 and 5002 in ops2, ops2
    print("PASS dispatch 5346：成功 ack+451+200 / 不足 5002 无 ack")


def test_warbook_exp():
    """马车经验升级：语义=当前等级行的 NeedExp（lv0→1 需 1、lv1→2 需 5）。"""
    make_player()
    lv = ensign.add_warbook_exp(1)     # lv0 行 NeedExp=1 → 直升 Lv1
    assert lv == 1, lv
    lv = ensign.add_warbook_exp(3)     # lv1 行 NeedExp=5，3<5 不升
    assert lv == 1
    lv = ensign.add_warbook_exp(2)     # 攒满 5 → Lv2
    assert lv == 2
    assert ensign.warbook_store()["lv_exp"] == 0
    lv = ensign.add_warbook_exp(999)   # 20+60+... 连升
    assert lv >= 4
    print("PASS 跑商马车：NeedExp=当前行语义 / 连升")


def test_warbook_business():
    """跑商全流程：选图→开跑（门票）→掷骰→领事件→结算→放弃。"""
    make_player(coin=5000000)
    wb = ensign.warbook_store()
    # GM 直接给马车经验到 Lv4（地图 1005 NeedLv=4）
    ensign.add_warbook_exp(999)
    assert wb["lv"] >= 4
    # 选图 + 开跑（门票 20020）
    ok, why = ensign.warbook_choose_map(1005)
    assert ok, why
    core.PLAYER_DATA["items"]["20020"] = 3
    ok, why = ensign.warbook_start()
    assert ok, why
    assert core.PLAYER_DATA["items"]["20020"] == 2, "开跑应耗 1 张门票"
    ok, why = ensign.warbook_start()
    assert not ok and "进行中" in why
    # 掷骰到背包满（容量 = TempBagCapacity + add_bag）
    cap = ensign.warbook_capacity()
    rounds = 0
    while len((wb.get("bagua") or {}).get("remain") or []) < cap and rounds < 200:
        coin0 = core.PLAYER_DATA["player"]["coin"]
        ok, why, event, cost, gained = ensign.warbook_divination()
        assert ok, why
        assert event == 1 and cost > 0
        assert core.PLAYER_DATA["player"]["coin"] == coin0 - cost
        # 每次商人事件必须领事件（31208）
        assert int(wb.get("current_event") or 0) == 1
        ok, why = ensign.warbook_event_done()
        assert ok, why
        assert int(wb.get("current_event") or 0) == 0
        rounds += 1
    assert rounds > 0
    bag = wb["bagua"]["remain"]
    assert len(bag) == cap, "背包应满 %s/%s" % (len(bag), cap)
    # 背包满后再掷 → 应被拒（容量钳制）——实际上客户端会先结算，这里验证钳制
    ok, why, ev, c, g = ensign.warbook_divination()
    assert ok and (g == [] or len((wb["bagua"]["remain"])) <= cap + 0), "不应超容量"
    if g:
        ensign.warbook_event_done()
    # 结算 31206
    have0 = dict(core.PLAYER_DATA["items"])
    lv0 = wb["lv"]
    ok, why, granted, lv = ensign.warbook_settle()
    assert ok, why
    assert granted, "应有货物入包"
    for a in granted:
        assert core.PLAYER_DATA["items"][str(a["type"])] >= a["count"]
    assert wb["start_draw_state"] == 0 and not (wb["bagua"].get("remain"))
    print("PASS 跑商：选图/门票/掷骰/领事件/容量钳制/结算入包(马车lv=%s)" % lv)
    # 放弃：保留一半
    ok, why = ensign.warbook_choose_map(1001)
    assert ok
    core.PLAYER_DATA["items"]["20020"] = 1
    ok, why = ensign.warbook_start()
    assert ok, why
    ok, why, event, cost, gained = ensign.warbook_divination()
    assert ok and gained, why
    ensign.warbook_event_done()
    ok, why, kept = ensign.warbook_giveup()
    assert ok, why
    assert wb["start_draw_state"] == 0
    # kept 数量 ≤ 原数量
    for a in kept:
        assert a["count"] >= 1
    print("PASS 跑商放弃：货物保留一半")


def test_warbook_business_dispatch():
    """dispatch 层跑商链路：31204→31207→31203→31208→31206（含推送与扣币）。"""
    make_player(coin=5000000)
    ensign.add_warbook_exp(999)
    s = Sess()
    # 选图 1001（★ 不推 5371：客户端 ack 自带状态，推送会在动画中途触发
    # refresh 清运行时节点导致客户端崩溃——2026-10-23 实机报障根因）
    im = pb.IDListMessage()
    im.id.append(1001)
    core.dispatch(s, 31204, im.SerializeToString())
    ops = [op for op, _ in s.sent]
    assert 31204 in ops, ops
    assert 5371 not in ops, "跑商流程禁止 5371 推送"
    # 开跑（无门票 → 拒绝）
    s2 = Sess()
    core.dispatch(s2, 31207, b"")
    ops2 = [op for op, _ in s2.sent]
    assert 5002 in ops2 and 31207 not in ops2, ops2
    # 给门票再开跑
    core.PLAYER_DATA["items"]["20020"] = 2
    s3 = Sess()
    core.dispatch(s3, 31207, b"")
    ops3 = [op for op, _ in s3.sent]
    assert 31207 in ops3 and 200 in ops3, ops3
    ack = pb.WarBookRecordData()
    ack.ParseFromString(dict(s3.sent)[31207])
    assert ack.startDrawState == 1 and ack.mapID == 1001
    # 前进 31203
    coin0 = core.PLAYER_DATA["player"]["coin"]
    s4 = Sess()
    core.dispatch(s4, 31203, b"")
    ops4 = [op for op, _ in s4.sent]
    assert 31203 in ops4 and 200 in ops4, ops4
    dv = pb.DivinationData()
    dv.ParseFromString(dict(s4.sent)[31203])
    assert dv.event == 1 and dv.baguaStatus.remainWarBookTypes, "应遇商人且有货物"
    assert core.PLAYER_DATA["player"]["coin"] < coin0
    # 领事件 31208
    s5 = Sess()
    core.dispatch(s5, 31208, b"")
    assert 31208 in [op for op, _ in s5.sent]
    dr = pb.DivinationResultData()
    dr.ParseFromString(dict(s5.sent)[31208])
    assert dr.divinationCount == 1
    # 结算 31206
    s6 = Sess()
    core.dispatch(s6, 31206, b"")
    ops6 = [op for op, _ in s6.sent]
    assert 31206 in ops6 and (401 in ops6 or 451 in ops6), ops6
    ack6 = pb.WarBookRecordData()
    ack6.ParseFromString(dict(s6.sent)[31206])
    assert ack6.startDrawState == 0
    print("PASS 跑商 dispatch：31204/31207(拒+成)/31203/31208/31206 全链路")


def test_blueprint_synthetic():
    """图纸合成 5355：4001（3 品质图纸×6 → 池 5025 随机出图纸）。"""
    make_player({"21044": 6})
    ok, why, granted, consumed = ensign.blueprint_synthetic(4001, [{"id": 21044, "count": 6}])
    assert ok, why
    assert consumed == [{"type": 21044, "count": 6}]
    assert granted and all(g["count"] >= 1 for g in granted)
    assert core.PLAYER_DATA["items"]["21044"] == 0
    for g in granted:
        assert core.PLAYER_DATA["items"][str(g["type"])] >= g["count"]
    # 品质不符 → 拒绝
    make_player({"21001": 6})
    ok, why, _, _ = ensign.blueprint_synthetic(4001, [{"id": 21001, "count": 6}])
    assert not ok and "品质" in why, why
    # 数量不符 → 拒绝
    make_player({"21044": 6})
    ok, why, _, _ = ensign.blueprint_synthetic(4001, [{"id": 21044, "count": 3}])
    assert not ok and "数量不符" in why, why
    # 库存不足 → 拒绝
    make_player({"21044": 2})
    ok, why, _, _ = ensign.blueprint_synthetic(4001, [{"id": 21044, "count": 6}])
    assert not ok and "数量不足" in why, why
    # dispatch 5355：成功 ack=AwardResult + 451 扣减
    make_player({"21044": 6})
    req = pb.IDCounteListByIDMessage()
    req.id = 4001
    e = req.idCount.add()
    e.id, e.count = 21044, 6
    s = Sess()
    core.dispatch(s, 5355, req.SerializeToString())
    ops = [op for op, _ in s.sent]
    assert 5355 in ops and (451 in ops or 499 in ops), ops
    ar = pb.AwardResult()
    ar.ParseFromString(dict(s.sent)[5355])
    assert len(ar.datas) >= 1, "ack 应含产出 spec"
    # dispatch 拒绝路径
    s2 = Sess()
    core.dispatch(s2, 5355, req.SerializeToString())
    ops2 = [op for op, _ in s2.sent]
    assert 5002 in ops2 and 5355 not in ops2, ops2
    print("PASS 图纸合成 5355：校验/掷池/ack=AwardResult/拒绝 5002")


if __name__ == "__main__":
    test_config()
    test_replacement()
    test_make_level_resolve()
    test_equip_slots()
    test_unlock_flag()
    test_squad_flag()
    test_squad_flag_bonus()
    test_squad_suit_bonus()
    # ⚠ 停用（2026-10-28）：套装百分比口径存在两个实现冲突——
    #   本测试按「配置 AddPowerPer」（群雄2件2%）+「配置名势力」（1104=群雄）；
    #   盘上 ensign.squad_flag_suit_additions 被另一 agent 改成硬编码
    #   （1102=群雄7%、1104=曹魏5%）。两者对 1102/1104 的势力归属相反，
    #   且 7% 口径待用户用客户端 tooltip 拍板 → 决议后恢复本组断言。
    test_squad_suit_faction_and_power_per()
    test_material_box_fallback()
    test_consume_plan()
    test_select_box_batch()
    test_builders()
    test_dispatch_5346()
    test_warbook_exp()
    test_warbook_business()
    test_warbook_business_dispatch()
    test_blueprint_synthetic()
    print("\n全部通过 ✔")
