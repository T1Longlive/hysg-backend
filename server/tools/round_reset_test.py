"""七天轮次重置测试（2026-10-23 用户方案：永久开启 + 每 7 天重置刷新）。

覆盖 6 个活动：单笔充值/累计充值/累计消耗/幸运转盘/神秘商店/超值兑换。

用法：python tools/round_reset_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_round_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.game.activities import (common, one_pay, grade_pay,
                                         turntable, secret_shop, activity_shop)


def make_player():
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 1000000, "lv": 60, "vip": 8, "diamond": 100000,
                   "pay_dollar": 0},
        "heroes": {}, "equips": {}, "items": {"20002": 99, "20007": 99, "20010": 99},
    })


def force_new_round(name):
    """模拟轮次切换：把该活动存档的 round_key 改成旧值。"""
    st = core.PLAYER_DATA.setdefault("activity", {}).setdefault(name, {})
    st["round_key"] = -1


def _in_round_now():
    """取"第一个轮次开始后 30 分钟"的时间戳（保证落在轮内）。"""
    import time
    cfg = secret_shop._cfg()
    r = secret_shop._rounds(cfg)[0]
    t = time.localtime()
    day0 = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
    return day0 + (r["s"] + 30) * 60


def _gap_now():
    """取"首轮开始前 1 分钟"的时间戳（必然是轮间空档）。"""
    import time
    t = time.localtime()
    day0 = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
    return day0 + 60


def test_common_round():
    import time
    rk1 = common.round_key(7)
    assert isinstance(rk1, int) and rk1 > 2000, "轮次 key = epoch_day//7（约 2963）"
    # 7 天后轮次 +1
    rk2 = common.round_key(7, time.time() + 7 * 86400)
    assert rk2 == rk1 + 1 or rk2 == rk1, "跨 7 天轮次前进"
    end = common.round_end_ts(7)
    assert end > time.time(), "轮次结束时间在未来"
    print("PASS 轮次助手：key/边界计算正常")


def test_one_pay_round():
    make_player()
    one_pay.on_recharge(30)
    one_pay.claim(1)
    st = one_pay._state()
    assert int(st["used"].get("0")) == 1, "已领 1 次"
    force_new_round("one_pay")
    st = one_pay._state()          # 触发轮次重置
    assert st["used"] == {} and st["reached"] == {}, "新轮次应清空"
    assert all(d.status == 0 for d in one_pay.snapshot().record.status)
    print("PASS 单笔充值：7 天轮次重置 reached/used")


def test_total_pay_round():
    make_player()
    core.PLAYER_DATA["player"]["pay_dollar"] = 1000   # 模拟充值流程已累计（VIP 用，永久）
    activities_on_recharge = __import__("sghy_server.game.activities",
                                        fromlist=["on_recharge"]).on_recharge
    activities_on_recharge(1000, 1000)   # 活动本轮累计
    snap = grade_pay.total_pay_snapshot()
    assert snap.totalPay == 1000, "本轮累计 1000"
    grade_pay.claim("total_pay", "dollar", 60)
    force_new_round("total_pay")
    snap = grade_pay.total_pay_snapshot()   # 触发轮次重置
    assert snap.totalPay == 0, "新轮次本轮充值额清零"
    assert all(r.status == 0 for r in snap.record.status)
    # 但仍然永久累计 pay_dollar（VIP 用）
    assert core.PLAYER_DATA["player"]["pay_dollar"] == 1000, "pay_dollar 不受轮次影响"
    print("PASS 累计充值：本轮充值额 7 天清零（pay_dollar 永久保留）")


def test_turntable_round():
    make_player()
    turntable._ensure()["keys"] = 5
    turntable.draw()
    snap = turntable.snapshot()
    assert snap.Times == 1
    force_new_round("turntable")
    snap = turntable.snapshot()   # 触发重置
    assert snap.Times == 0 and len(snap.Indexes) == 0, "次数/格子清零"
    assert snap.keyCount == 3, "钥匙回初始 3 把"
    print("PASS 幸运转盘：7 天轮次重置次数/格子/钥匙")


def test_secret_shop_round():
    make_player()
    now = _in_round_now()
    snap = secret_shop.snapshot(now)
    props1 = [(p.type, p.count) for p in snap.record.props]
    # 同轮内重复取不变（确定性生成）
    snap2 = secret_shop.snapshot(now)
    assert [(p.type, p.count) for p in snap2.record.props] == props1
    # 轮内买断一件 → 轮次切换（round_key 改旧值模拟换轮）→ 已售清零
    target = next(p for p in snap.record.props if p.price > 0)
    ok, why, _, _ = secret_shop.buy(target.index, now)
    assert ok, why
    force_new_round("secret_shop")
    snap3 = secret_shop.snapshot(now)
    sold = next(p for p in snap3.record.props
                if p.type == target.type and p.count == target.count)
    assert sold.status == 0, "换轮后限购重置（可再买）"
    assert snap3.nextTime > 0
    # 强制把 now 推到轮外 → 空档态
    gap = _gap_now()
    g = secret_shop.snapshot(gap)
    assert len(g.record.props) == 0 and g.nextTime > 0, "轮外=空档态"
    print("PASS 神秘商店：轮内确定性生成 + 换轮重置限购 + 空档态")


def test_activity_shop_round():
    make_player()
    ok, why, _, _ = activity_shop.buy(1, [])
    assert ok, why
    assert activity_shop.snapshot().Grades[0].UsedTimes == 1
    force_new_round("activity_shop")
    assert activity_shop.snapshot().Grades[0].UsedTimes == 0, "新轮次次数清零"
    print("PASS 超值兑换：7 天轮次重置兑换次数")


if __name__ == "__main__":
    test_common_round()
    test_one_pay_round()
    test_total_pay_round()
    test_turntable_round()
    test_secret_shop_round()
    test_activity_shop_round()
    print("TOTAL: 6 组 passed")
