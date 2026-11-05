"""武将突破等级上限回归（2026-10-28）。

用户报障：无双吕布突破后等级上限仍显示 40，而周瑜 40→45 正常。

根因：存档 `heroes.can_break` 是**过期的**（id=97 无双吕布 =0），服务端算
`level_cap(quality, awaken, can_break)` 时信了存档 → 40+5×1 被 can_break=False
压回 40；客户端 `canBreakLv()` 读的是 HeroConfig.CanBreakLevel（10856=1），
所以突破按钮照常能点（do_awaken 校验也用配置，能突破成功），只是上限不动。

修法：can_break 一律以**配置**为准（启动自检纠正存档 + level_cap/add_exp/hero_msg
全部改读 growth.hero_can_break(type)）。

用法：python tools/hero_break_limit_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_hero_break_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sghy_server.game import builders_pb, core, growth

WUSHUANG_LUBU = 10856      # 无双吕布 6★（CanBreakLevel=1）
ZHOUYU = 10256             # 周瑜 6★（对照组）


def _hero(hid, typ, lv, awaken, can_break, star=6):
    return {"id": hid, "type": typ, "lv": lv, "grade": 0, "grade_exp": 0,
            "awaken": awaken, "star": star, "position": 1 if hid == 97 else 0,
            "can_break": can_break, "equips": [], "fate": {}}


def test_config_beats_stale_record():
    """存档 can_break=0 但配置=1 时，maxLv 必须按配置算（突破 +5/次）。"""
    growth.init()
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"lv": 60, "vip": 0},
        "heroes": {
            # 无双吕布：存档标志过期（0），已突破 1 次，Lv40
            "97": _hero(97, WUSHUANG_LUBU, 40, 1, 0),
            # 周瑜：存档标志正确（1），已突破 1 次
            "51": _hero(51, ZHOUYU, 40, 1, 1, star=6),
        },
        "items": {}, "equips": {}, "fate": {}, "levels": {}, "level_records": {},
    })

    # 配置层：两者都能突破
    assert growth.hero_can_break(WUSHUANG_LUBU), "配置里无双吕布必须可突破"
    assert growth.hero_can_break(ZHOUYU), "配置里周瑜必须可突破"

    # hero_msg 下发的 maxLv：突破 1 次 -> 40+5 = 45（以前无双吕布信存档 =40）
    m1 = builders_pb.hero_msg(97, WUSHUANG_LUBU, 40, 1)
    m2 = builders_pb.hero_msg(51, ZHOUYU, 40, 1)
    assert m1.maxLv == 45, "无双吕布 maxLv 应 45，实际 %s" % m1.maxLv
    assert m2.maxLv == 45, "周瑜 maxLv 应 45，实际 %s" % m2.maxLv

    # 上限计算路径（升级/用符）同样按配置
    cap = growth.level_cap(6, 1, growth.hero_can_break(WUSHUANG_LUBU))
    assert cap == 45, cap
    print("PASS 配置压过存档：无双吕布/周瑜 maxLv 均 45（突破1次），level_cap=45")


def test_sanitize_fixes_stale_record():
    """启动自检必须纠正过期的 can_break（不是只补缺失）。"""
    growth.init()
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"lv": 60, "vip": 0, "coin": 6000000},
        "heroes": {"97": _hero(97, WUSHUANG_LUBU, 40, 1, 0)},
        "items": {}, "equips": {}, "fate": {}, "levels": {}, "level_records": {},
    })
    removed, fixed = core._sanitize_player_data()
    h = core.PLAYER_DATA["heroes"]["97"]
    assert bool(h["can_break"]) is True, \
        "自检应把过期的 can_break=0 纠正为 1，实际 %r" % h["can_break"]
    print("PASS 自检纠正过期 can_break：id=97 -> %r" % h["can_break"])


if __name__ == "__main__":
    test_config_beats_stale_record()
    test_sanitize_fixes_stale_record()
    print("\n全部通过 ✔")
