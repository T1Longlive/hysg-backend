"""装备洗练规则单元测试（2026-10-22）。

对齐客户端三条规则：
  1. 颜色分档：AdditionBtn:getColor 用 AdditionData.param2/param3（min/max 区间）——
     服务端必须随 pending/assist 下发洗练区间，否则永远最低档
  2. 保存门禁（CostGroup）：同属性且数值更低 → 只允许放弃；同属性更高 → 只允许保存；
     同属性相等 → 都可；不同属性 → 都可（服务端同样强制）
用法：python tools/equip_reform_unit_test.py
"""

import sys
import os
import tempfile

# ★ 必须在 import core 之前隔离存档路径（core 加载即可能 save_player_data）
os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_reform_unit_test_player.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.proto import ps_protobuf_pb2 as pb

TARGET = 33115   # q4 5星，两条副属性（slot1 需3星 / slot2 需5星）


def make_player(eq_extra=None):
    eq = {"id": 1, "type": TARGET, "host": 0, "strengthen": 0,
          "grade": 0, "grade_exp": 0}
    if eq_extra:
        eq.update(eq_extra)
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 9999999, "lv": 60, "vip": 0, "diamond": 9999},
        "heroes": {},
        "equips": {"1": eq},
        "items": {"20002": 9999},
    })
    return eq


def test_color_range():
    eq = make_player()
    ok, why, eq2 = core.do_reform_equip(1, 1, 60)
    assert ok, "洗练应成功: %s" % why
    pend = eq["pending_assist"]["1"]
    assert pend.get("min", 0) > 0 and pend.get("max", 0) >= pend["min"], \
        "pending 必须带 min/max 区间（颜色分档），实际 min=%s max=%s" % (
            pend.get("min"), pend.get("max"))
    # 下发的 lastUnlockAssistant 必须携带 param2/param3（客户端 getColor 用）
    e = pb.EquipData()
    e.ParseFromString(core.build_equip_data(eq))
    assert e.lastUnlockAssistant, "应有待保存副属性下发"
    a = e.lastUnlockAssistant[0]
    assert a.param2 == pend["min"] and a.param3 == pend["max"], \
        "AdditionData param2/param3 应=%d/%d，实际 %d/%d" % (
            pend["min"], pend["max"], a.param2, a.param3)
    print("PASS 颜色区间：min=%d max=%d 随 pending/lastUnlockAssistant 下发"
          % (pend["min"], pend["max"]))


def _save_case(cur_attr, cur_val, new_attr, new_val):
    eq = make_player({"assist": {"1": {"attr": cur_attr, "value": cur_val,
                                       "times": 1, "min": 1, "max": 999}},
                      "pending_assist": {"1": {"attr": new_attr, "value": new_val,
                                               "times": 1, "min": 1, "max": 999}}})
    return core.do_save_reform(1, 1)


def test_save_rules():
    # 同属性、更低 → 拒绝
    ok, why, _ = _save_case(1, 100, 1, 50)
    assert not ok and "低于当前" in why, "同属性更低应拒绝保存，实际 %s | %s" % (ok, why)
    print("PASS 保存门禁：同属性更低被拒（%s）" % why)

    # 同属性、更高 → 允许
    ok, why, eq = _save_case(1, 100, 1, 150)
    assert ok and int(eq["assist"]["1"]["value"]) == 150, "同属性更高应允许保存"
    print("PASS 保存门禁：同属性更高允许保存")

    # 同属性、相等 → 允许（客户端 SAVE_AND_GIVE_UP）
    ok, why, _ = _save_case(1, 100, 1, 100)
    assert ok, "同属性相等应允许保存"
    print("PASS 保存门禁：同属性相等允许保存")

    # 不同属性 → 允许
    ok, why, eq = _save_case(1, 100, 4, 10)
    assert ok and int(eq["assist"]["1"]["attr"]) == 4, "不同属性应允许保存"
    print("PASS 保存门禁：不同属性允许保存")


def test_assist_in_hero_stats():
    """副属性真正生效（2026-10-22）：已解锁/洗练的 assist 计入武将属性与战力。"""
    eq = {"id": 7, "type": TARGET, "host": 42, "strengthen": 0, "grade": 0,
          "assist": {"1": {"attr": 1, "value": 500, "times": 1}}}   # 攻击+500
    hero = {"id": 42, "type": 10016, "lv": 60, "grade": 0, "position": 1,
            "equips": [7], "fate": {}}
    make_player(eq_extra=None)
    core.PLAYER_DATA["equips"] = {"7": eq}
    core.PLAYER_DATA["heroes"] = {"42": hero}
    adds = core.equip_additions_for(hero)
    assert (1, 500, 0) in adds, "副属性(攻击+500)应计入装备加成: %s" % (adds,)
    st = core.hero_attrs(10016, 60, 0)
    base_atk = st["attack"]
    core.apply_fate_to_stats(st, hero)
    assert st["attack"] >= base_atk + 500, "攻击应含副属性 +500：%d -> %d" % (base_atk, st["attack"])
    print("PASS 副属性生效：攻击 +%d 计入（%d -> %d），战力 %s"
          % (500, base_atk, st["attack"], st.get("power")))


def test_type4_pool_unlock():
    """Type=4 装备（七星龙渊剑 34075）副属性可解锁（2026-10-28 报障回归）。

    根因：ProbabilitySetConfig 以前读 04_config_tables/json 的旧 JSON（只有
    Group 1/2），Group 3/4 全丢 → Type=3/4 的装备池空 → 报"玩家等级不够"。
    现在改读 rt_all 的 .new lua（Group 1-4）。Type 1/4=攻击系、2/3=防御系。
    """
    from sghy_server.game import growth
    T = 34075          # 七星龙渊剑：slot1/slot2 都是 Type=4
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 9999999, "lv": 60, "vip": 0, "diamond": 9999},
        "heroes": {}, "items": {},
        "equips": {"5": {"id": 5, "type": T, "host": 0, "strengthen": 0,
                         "grade": 9, "grade_exp": 0}},
    })
    growth._load_prob()
    groups = sorted({r["group"] for r in growth._prob_cfg})
    assert groups == [1, 2, 3, 4], "属性池必须含 Group 1-4（旧 JSON 只有 1/2）: %s" % groups
    assert growth.equip_reform_pool(4, 60), "Type=4（攻击二套）池不能为空"

    ok, why, eq = core.do_unlock_assistant(5, 1, 60)
    assert ok, "解锁副属性必须成功，实际被拒：%s" % why
    assist = (eq.get("assist") or {}).get("1")
    assert assist and assist["attr"] > 0, assist
    assert assist["attr"] in (3, 12, 22), "Type=4 池只出 速度(3)/最终伤害(12)/命中(22): %s" % assist
    # 元宝已扣
    assert core.PLAYER_DATA["player"]["diamond"] == 9999 - 300
    print("PASS Type=4 装备解锁副属性：池含 Group1-4、属性=%s 值=%s、扣元宝300"
          % (assist["attr"], assist["value"]))


def test_special_attrs_reach_hero_data():
    """增伤/减伤等特殊属性必须进 stats 并随 HeroData 下发（2026-10-28 报障）。

    链路：装备副属性 assist → equip_additions_for → apply_addition_bonus
    → stats.finalDamage/damageReduce/... → hero_msg → HeroData(22-29/32/35/36)
    战斗入场（BattleScenePVE 建队）读 WarriorData 的 damageIncrease 等，
    而 WarriorData 由 WarriorManager 从 **HeroData** 填充 → 此链路即"进战斗前生效"。
    """
    from sghy_server.game import builders_pb, growth
    growth.init()
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"lv": 60, "coin": 9999999, "vip": 8, "diamond": 9999},
        "heroes": {"7": {"id": 7, "type": 10856, "lv": 60, "grade": 0,
                         "position": 1, "equips": [55], "fate": {}}},
        "items": {},
        "equips": {"55": {"id": 55, "type": 34075, "host": 7, "strengthen": 0,
                          "grade": 0, "pending_assist": {},
                          "assist": {"1": {"attr": 12, "value": 20, "kind": 4, "times": 1},
                                     "2": {"attr": 13, "value": 15, "kind": 4, "times": 1}}}},
    })
    adds = core.equip_additions_for(core.PLAYER_DATA["heroes"]["7"])
    assert (12, 20, 0) in adds and (13, 15, 0) in adds, adds
    st = core.hero_attrs(10856, 60, 0)
    core.apply_fate_to_stats(st, core.PLAYER_DATA["heroes"]["7"])
    assert st.get("finalDamage") == 20, "增伤必须保留在 stats 里（旧实现在这里丢弃）: %s" % st
    assert st.get("damageReduce") == 15, st

    m = builders_pb.hero_msg(7, 10856, 60, 1)
    assert m.damageIncrease == 20, "HeroData.damageIncrease 必须下发: %s" % m.damageIncrease
    assert m.damageDecrease == 15, "HeroData.damageDecrease 必须下发: %s" % m.damageDecrease

    # 卸下装备 → 特殊属性回落 0（必须显式下发 0，否则客户端 WarriorData 保持旧值）
    core.PLAYER_DATA["heroes"]["7"]["equips"] = []
    m2 = builders_pb.hero_msg(7, 10856, 60, 1)
    assert m2.damageIncrease == 0 and m2.damageDecrease == 0, (m2.damageIncrease, m2.damageDecrease)
    print("PASS 特殊属性链路：副属性增伤+20/减伤+15 进 stats 与 HeroData，卸下回落 0")


def test_reform_consumption():
    """洗练必须正确扣道具（2026-10-28 报障回归）。

    根因：growth 的 ExpSetConfig 以前读 legacy_json 旧反编译，第 5 行
    EquipReformCoin/Stone 为空 -> _num('')=0 -> 5★ 洗练消耗 (0,0)，
    道具/铜币一分不扣。已切源 rt_all .new lua（同 ProbabilitySetConfig 修法）。
    口径对齐客户端 CostGroup:updateData：
      stone = EquipReformStone（固定）
      coin  = EquipReformCoin × (玩家等级 + times)，每次洗练 times+1
    """
    from sghy_server.game import growth
    growth.init()
    # 1) 星级消耗表必须来自真配置（旧 JSON 第 5 行是全空）
    assert growth.equip_reform_cost(2) == (50, 1), growth.equip_reform_cost(2)
    assert growth.equip_reform_cost(3) == (150, 2), growth.equip_reform_cost(3)
    assert growth.equip_reform_cost(4) == (450, 3), growth.equip_reform_cost(4)
    assert growth.equip_reform_cost(5) == (450, 3), growth.equip_reform_cost(5)
    # 2) 配置源锁定：BuyEp 17 行起旧 JSON 为空（真配置 100）
    assert growth._row_at(20).get("BuyEp") == 100, growth._row_at(20).get("BuyEp")

    # 3) 5★ 装备（33115）已解锁第 1 条：洗练实扣 3 石 + 450×(60+1) 币
    eq = make_player({"assist": {"1": {"attr": 1, "value": 100, "kind": 1,
                                       "times": 1, "min": 10, "max": 30}}})
    coin0 = core.PLAYER_DATA["player"]["coin"]
    stone0 = core.PLAYER_DATA["items"]["20002"]
    ok, why, eq = core.do_reform_equip(1, 1, 60)
    assert ok, "洗练应成功: %s" % why
    assert core.PLAYER_DATA["items"]["20002"] == stone0 - 3, "必须扣 3 个洗练石"
    assert core.PLAYER_DATA["player"]["coin"] == coin0 - 450 * 61, \
        "铜币必须按 450×(60+1) 扣：%s" % why
    # 4) 保存后 times=2，下一次洗练涨到 450×(60+2)
    ok, why, _ = core.do_save_reform(1, 1)
    assert ok and int(eq["assist"]["1"]["times"]) == 2, eq["assist"]["1"]
    coin1 = core.PLAYER_DATA["player"]["coin"]
    ok, why, _ = core.do_reform_equip(1, 1, 60)
    assert ok, why
    assert core.PLAYER_DATA["player"]["coin"] == coin1 - 450 * 62, why
    # 5) 洗练石不足必须被拒且不扣（元宝也清零 → 纯拒绝路径）
    core.PLAYER_DATA["items"]["20002"] = 2
    core.PLAYER_DATA["player"]["diamond"] = 0
    coin2 = core.PLAYER_DATA["player"]["coin"]
    ok, why, _ = core.do_reform_equip(1, 1, 60)
    assert not ok and "洗练石不足" in why, (ok, why)
    assert core.PLAYER_DATA["player"]["coin"] == coin2, "被拒不能扣币"
    print("PASS 洗练消耗：5★ 每次 3 石 + 450×(lv+times) 币，不足被拒不扣")


def test_reform_diamond_supplement():
    """洗练石不足 → 元宝补充（2026-10-28 用户报障：弹了元宝补充，点确认还是失败）。

    客户端 CostGroup:requestReform 原版逻辑：石头不够 → 弹「洗练石不足，
    是否花费 (100+times) 元宝购买洗练石？」→ 确认后**原样重发 6025**
    （请求无任何标记）。服务端必须自己推断：石头不够但元宝够 → 扣
    (100+times) 元宝代付，照常完成洗练。以前直接 5002 → 点确认必失败。
    """
    # 1) 0 石头 + 元宝够 → 走补充：扣 100+times 元宝，石头不动，币照扣
    eq = make_player({"assist": {"1": {"attr": 1, "value": 100, "kind": 1,
                                       "times": 1, "min": 10, "max": 30}}})
    core.PLAYER_DATA["items"]["20002"] = 0
    d0 = core.PLAYER_DATA["player"]["diamond"]
    c0 = core.PLAYER_DATA["player"]["coin"]
    ok, why, eq = core.do_reform_equip(1, 1, 60)
    assert ok, "元宝补充后洗练应成功: %s" % why
    assert "元宝" in why, why
    assert core.PLAYER_DATA["player"]["diamond"] == d0 - 101, "应扣 100+times=101 元宝"
    assert core.PLAYER_DATA["items"]["20002"] == 0, "走元宝补充不扣石头"
    assert core.PLAYER_DATA["player"]["coin"] == c0 - 450 * 61, "铜币照扣"
    print("PASS 元宝补充：0 石头 → 扣 101 元宝代付，洗练成功（币照扣 27450）")

    # 2) 0 石头 + 元宝不够 → 拒绝且分文不扣
    core.PLAYER_DATA["items"]["20002"] = 0
    core.PLAYER_DATA["player"]["diamond"] = 50
    d1, c1 = core.PLAYER_DATA["player"]["diamond"], core.PLAYER_DATA["player"]["coin"]
    ok, why, _ = core.do_reform_equip(1, 1, 60)
    assert not ok and "洗练石不足" in why and "元宝补充需 101" in why, (ok, why)
    assert (core.PLAYER_DATA["player"]["diamond"],
            core.PLAYER_DATA["player"]["coin"]) == (d1, c1), "被拒不能扣任何资源"
    print("PASS 元宝也不够：拒绝且不扣（%s）" % why)

    # 3) 部分石头（1/3）+ 元宝够 → 也走补充，剩余石头保留
    core.PLAYER_DATA["items"]["20002"] = 1
    core.PLAYER_DATA["player"]["diamond"] = 9999
    ok, why, _ = core.do_reform_equip(1, 1, 60)
    assert ok, why
    assert core.PLAYER_DATA["items"]["20002"] == 1, "元宝代付不动已有石头"
    assert core.PLAYER_DATA["player"]["diamond"] == 9999 - 101
    print("PASS 部分石头：1/3 → 元宝代付，剩余 1 个石头保留")


if __name__ == "__main__":
    test_color_range()
    test_save_rules()
    test_assist_in_hero_stats()
    test_type4_pool_unlock()
    test_special_attrs_reach_hero_data()
    test_reform_consumption()
    test_reform_diamond_supplement()
    print("TOTAL: 6 组（20 条断言）passed")
