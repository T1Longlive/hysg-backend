"""缘分加成单元测试（2026-10-28）。

覆盖一条真实报障：赵云 6★ 戴「神龙胆亮银枪」后攻击力 12162 而不是 9658。
根因：装备缘（FateConfig.kind=1）如果同时出现在武将 CanRelation 里，
首段（hero_fate_ids 遍历）已生效加入，但**没有登记 own_done** →
后面的「装备缘兜底（按 Owner 扫描）」再次扫到同一 fid 重复叠加 →
+35% 变成 +70%。

断言：
  1. 装备缘只生效一次（加成条目里不重复）
  2. 面板攻击 = (成长基础 + 装备固定值) × (1 + 缘分%)
  3. 未穿戴该装备时不生效
  4. 永久激活的缘分仍对所有同名实例生效（回归保护）

用法：python tools/fate_bonus_unit_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(
    tempfile.gettempdir(), "sghy_fate_bonus_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sghy_server.game import core, growth

HERO_TYPE = 10146       # 赵云 6★（Group=14，CanRelation 含装备缘 1139）
WEAPON_TYPE = 33534     # 神龙胆亮银枪（EquipConfig.Group=313，攻击 +856）


def _setup(equipped=True):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"lv": 80, "vip": 0},
        "heroes": {"23": {"id": 23, "type": HERO_TYPE, "lv": 80, "grade": 9,
                          "position": 0, "equips": [700] if equipped else [],
                          "fate": {}}},
        "items": {},
        "equips": {"700": {"id": 700, "type": WEAPON_TYPE, "host": 23 if equipped else 0,
                           "strengthen": 0, "grade": 0}},
        "fate": {},
    })
    return core.PLAYER_DATA["heroes"]["23"]


def test_equip_fate_not_duplicated():
    h = _setup(equipped=True)
    fids = growth.hero_fate_ids(HERO_TYPE)
    equip_fids = [f for f in fids if growth.fate_kind(f) == 1]
    assert equip_fids, "赵云 CanRelation 里应有装备缘（这是本次 bug 的前提）"

    bon = core.fate_bonuses_for(h)
    # 同一 fid 的加成不能出现两次（历史上这里重复了）
    from collections import Counter
    dup = [k for k, c in Counter(bon).items() if c > 1]
    assert not dup, "缘分加成重复（%s）：%s" % (dup, bon)

    # 装备缘的攻击百分比只应有一份
    atk_pct = [v for t, v, m in bon if t == 1 and m == 1]
    assert atk_pct == [35.0], "攻击缘分百分比应只有一条 35%%，实际 %s" % atk_pct

    # 面板攻击 = (基础 + 装备) × (1 + 35%)
    base = growth.hero_attrs(HERO_TYPE, 80, 9)
    weapon_atk = [v for t, v, m in core.equip_additions_for(h) if t == 1 and m == 0]
    st = dict(base)
    core.apply_fate_to_stats(st, h)
    expect = int(round((base["attack"] + sum(weapon_atk)) * 1.35))
    assert abs(st["attack"] - expect) <= 2, \
        "攻击 %d ≠ 期望 %d（基础 %d + 武器 %s）×1.35" % (
            st["attack"], expect, base["attack"], weapon_atk)
    print("PASS 装备缘不重复：攻击缘分 35%% 只生效一次，面板 %d = (%d+%s)×1.35"
          % (st["attack"], base["attack"], weapon_atk))


def test_equip_fate_only_when_worn():
    h = _setup(equipped=False)
    bon = core.fate_bonuses_for(h)
    atk_pct = [v for t, v, m in bon if t == 1 and m == 1]
    assert atk_pct == [], "没穿装备缘装备时不应有 35%% 攻击加成: %s" % atk_pct
    print("PASS 装备缘按穿戴生效：未穿戴 → 无 35% 攻击加成")


if __name__ == "__main__":
    growth.init()
    test_equip_fate_not_duplicated()
    test_equip_fate_only_when_worn()
    print("\n全部通过 ✔")
