"""第二批活动单元测试（2026-10-23）：幸运转盘 / 神秘商店 / 活动商店。

用法：python tools/activity_batch2_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_activity_b2_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.game.activities import turntable, secret_shop, activity_shop, build_activity_group
from sghy_server.proto import ps_protobuf_pb2 as pb


def make_player():
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 1000000, "lv": 60, "vip": 8, "diamond": 100000},
        "heroes": {}, "equips": {}, "items": {"20002": 99, "20007": 99, "20010": 99},
    })


def test_turntable():
    make_player()
    snap = turntable.snapshot()
    assert snap.ID > 0 and len(snap.Cells) == 14, "14 格"
    assert snap.keyCount == 3 and snap.currency == 2 and snap.price == 50
    # 抽 3 次（3 把钥匙）
    for _ in range(3):
        ok, why, idx0, award, keys = turntable.draw()
        assert ok, why
        assert 0 <= idx0 < 14 and award is not None
    # 钥匙用完
    ok, why, _, _, keys = turntable.draw()
    assert not ok and "钥匙不足" in why, why
    # 买钥匙
    ok, why, keys = turntable.buy_keys(2)
    assert ok and keys == 2, (ok, why, keys)
    assert core.PLAYER_DATA["player"]["diamond"] == 100000 - 100
    # 再抽
    ok, why, idx0, award, keys = turntable.draw()
    assert ok, why
    snap = turntable.snapshot()
    assert snap.Times == 4 and len(snap.Indexes) == 4
    print("PASS 幸运转盘：14 格下发 + 抽奖扣钥匙 + 钥匙不足拒 + 元宝买钥匙")


def test_turntable_all_drawn():
    make_player()
    st = turntable._ensure()
    st["keys"] = 100
    for _ in range(14):
        ok, why, *_ = turntable.draw()
        assert ok, why
    ok, why, *_ = turntable.draw()
    assert not ok and "全部领取" in why, why
    print("PASS 幸运转盘：14 格抽完封顶")


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


def test_secret_shop():
    make_player()
    now = _in_round_now()
    snap = secret_shop.snapshot(now)
    assert snap.isStart == 1, "活动开启"
    assert len(snap.timeRegion) == 3, "timeRegion 应下发 3 轮时间表（规则弹窗）"
    assert snap.timeRegion[0].startTime == "10:00", '轮次表用 "HH:MM" 字符串'
    assert len(snap.record.props) == 8, "每轮 8 格商品"
    assert len([p for p in snap.record.props if p.price == 0]) == 0, \
        "free_slots=0 应无免费商品"
    paid = [p for p in snap.record.props if p.price > 0]
    assert all(p.originalPrice > p.price for p in paid), "收费商品应显示原价折扣"
    assert snap.nextTime > 0, "nextTime=本轮关闭倒计时"
    # 买一个收费商品（轮内限购一次）
    target = paid[0]
    before = core.PLAYER_DATA["player"]["diamond"]
    ok, why, awards, prop = secret_shop.buy(target.index, now)
    assert ok, why
    assert core.PLAYER_DATA["player"]["diamond"] == before - target.price
    snap2 = secret_shop.snapshot(now)
    sold = next(p for p in snap2.record.props if p.index == target.index)
    assert sold.status == 1, "买后已售"
    # 重复买拒绝
    ok, why, _, _ = secret_shop.buy(target.index, now)
    assert not ok and "售空" in why, why
    # 轮间空档：无商品 + 下轮开启倒计时 + 拒买
    g = secret_shop.snapshot(_gap_now())
    assert len(g.record.props) == 0, "空档 props 应为空（客户端显示商人图）"
    assert g.nextTime > 0, "空档 nextTime=下轮开启倒计时"
    ok, why, _, _ = secret_shop.buy(1, _gap_now())
    assert not ok and "下一轮" in why, why
    print("PASS 神秘商店：3 轮时间表 + 每轮 8 格全收费 + 轮内限购 + 空档态/拒买")


def test_activity_shop():
    make_player()
    snap = activity_shop.snapshot()
    assert snap.isStart == 1 and len(snap.Grades) == 45, "兑换档位 45 个"
    g1 = next(g for g in snap.Grades if g.Grade == 1)
    assert g1.CheckTimes == 99 and g1.UsedTimes == 0 and not g1.IsLimit
    assert len(g1.ResultAward) == 1, "grade1 兑换资源 1 条奖励"
    # 兑换（元宝 2000）
    before = core.PLAYER_DATA["player"]["diamond"]
    ok, why, awards, body = activity_shop.buy(1, [])
    assert ok, why
    assert core.PLAYER_DATA["player"]["diamond"] == before - 200
    m = pb.GradeExchangeData(); m.ParseFromString(body)
    assert m.Grade == 1 and m.UsedTimes == 1, "回包 Grade 回显 + 次数"
    # grade1 不限购（99 次）：连兑 3 次都成功
    for _ in range(2):
        ok, why, _, _ = activity_shop.buy(1, [])
        assert ok, why
    # 元宝不足
    core.PLAYER_DATA["player"]["diamond"] = 10
    ok, why, _, _ = activity_shop.buy(3, [])
    assert not ok and "不足" in why, why
    print("PASS 活动商店：6 档 + Grade回显 + 不限购连兑 + 元宝不足拒")


def test_group_assembly():
    import time
    make_player()
    m = pb.ActivityGroupData()
    m.ParseFromString(build_activity_group())
    assert m.lotteryDrawActivity.ID > 0, "转盘 ID>0 才显示入口"
    assert len(m.lotteryDrawActivity.Cells) == 14
    assert m.SecretShop.isStart == 1, "神秘商店开启"
    assert len(m.SecretShop.timeRegion) == 3, "轮次时间表随包下发"
    # 轮内=8 格商品；轮间空档=0 格（真实当前时间，两种都合法）
    loc = secret_shop._locate(secret_shop._cfg(), int(time.time()))
    expect = 8 if loc["phase"] == "in" else 0
    assert len(m.SecretShop.record.props) == expect, \
        "轮内 8 格 / 空档 0 格（当前 %s）" % loc["phase"]
    assert m.activityShopData.isStart == 1 and len(m.activityShopData.Grades) == 45
    print("PASS 5311 组装：转盘/神秘商店/活动商店三活动齐下发")


if __name__ == "__main__":
    test_turntable()
    test_turntable_all_drawn()
    test_secret_shop()
    test_activity_shop()
    test_group_assembly()
    print("TOTAL: 5 组 passed")
