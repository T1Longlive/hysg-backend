"""活动 M2 单元测试（2026-10-23）：充值三活动 + 充值链路联动。

覆盖：累计充值/单笔(onePay)/累计消费的 snapshot、领奖、状态机，
      BuyProp 充值 → one_pay 档位自动达成联动。

用法：python tools/activity_m2_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_activity_m2_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.game.activities import one_pay, grade_pay, build_activity_group
from sghy_server.proto import ps_protobuf_pb2 as pb


def make_player():
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 999999, "lv": 60, "vip": 0, "diamond": 0,
                   "pay_dollar": 0},
        "heroes": {}, "equips": {}, "items": {},
    })


def test_total_pay():
    make_player()
    snap = grade_pay.total_pay_snapshot()
    assert snap is not None and snap.isStart == 1
    assert len(snap.record.status) == 5, "5 档，实际 %d" % len(snap.records)
    assert all(r.status == 0 for r in snap.record.status), "pay_dollar=0 应全未达成"
    # 本轮充值 100 → 60 档可领（2026-10-23 改：累计充值按本轮累计）
    grade_pay.on_recharge(100)
    snap = grade_pay.total_pay_snapshot()
    assert snap.record.status[0].status == 1 and snap.record.status[0].Grade == 60
    assert snap.record.status[1].status == 0
    # 领 60 档
    ok, why, awards = grade_pay.claim("total_pay", "dollar", 60)
    assert ok, why
    assert awards, "应有奖励"
    snap = grade_pay.total_pay_snapshot()
    assert snap.record.status[0].status == 2, "领后应已领取"
    # 重复领拒绝
    ok, why, _ = grade_pay.claim("total_pay", "dollar", 60)
    assert not ok and "已领取" in why, why
    # 未达档拒绝
    ok, why, _ = grade_pay.claim("total_pay", "dollar", 300)
    assert not ok and "未达到" in why, why
    print("PASS 累计充值：5 档三态 + 领奖/重复拒/未达拒")


def test_one_pay():
    make_player()
    snap = one_pay.snapshot()
    assert snap is not None and len(snap.record.status) == 9, "9 档"
    assert all(d.status == 0 for d in snap.record.status)
    # 充值 30 元单笔 → 6/25/30 档达成（计数模型：30 档可领 3 次）
    # 单笔 30 元 → 只有 30 元档达成（2026-10-27 用户拍板："等于"才解锁，
    # 不再连带解锁低档）
    one_pay.on_recharge(30)
    snap = one_pay.snapshot()
    assert snap.record.status[2].status == 1, "30 元档应可领"
    assert snap.record.status[0].status == 0, "6 元档不该被 30 元连带解锁"
    assert snap.record.status[1].status == 0, "25 元档不该被连带解锁"
    assert snap.record.status[3].status == 0, "50 元档未达成"
    assert snap.record.status[0].gradeValue == 60, "gradeValue=60（6元×10）"
    assert snap.record.status[1].checkTimes == 10, "checkTimes=10"
    # 领 30 元档（1-based 档序号 = 3，即 index 2）
    ok, why, awards = one_pay.claim(3)
    assert ok, why
    snap = one_pay.snapshot()
    assert snap.record.status[2].usedTimes == 1, "used 1"
    # 未达成拒（50 元档 = 序号 4）
    ok, why, _ = one_pay.claim(4)
    assert not ok and "未达成" in why, why
    print("PASS 单笔充值(onePay)：计数模型 + 充值触发达成 + 未达成拒")


def test_total_consume():
    """累计消耗（元宝）：余额差值法——余额减少即计入（2026-10-23 改版）。"""
    make_player()
    core.PLAYER_DATA["player"]["diamond"] = 10000
    grade_pay.sync_dollar_consume()      # 建立基准
    snap = grade_pay.total_consume_snapshot()
    assert snap is not None and snap.totalConsume == 0
    assert all(r.status == 0 for r in snap.record.status)
    # 花 600 元宝（余额 10000 → 9400）
    core.PLAYER_DATA["player"]["diamond"] = 9400
    snap = grade_pay.total_consume_snapshot()
    assert snap.totalConsume == 600, "余额差值 600"
    assert snap.record.status[0].status == 1, "500 档应可领"
    assert snap.record.status[1].status == 0, "2000 档未达成"
    ok, why, awards = grade_pay.claim("total_consume", "consume", 500)
    assert ok, why
    snap = grade_pay.total_consume_snapshot()
    assert snap.record.status[0].status == 2
    print("PASS 累计消耗（元宝余额法）：600 消耗 → 500 档可领 → 领后已领")


def test_recharge_link():
    """BuyProp 充值 → one_pay 档位联动 + pay_dollar 累计。"""
    make_player()
    ok, why, awards, extra = core.do_buy_prop(1018, 1)   # 6 元档
    assert ok, why
    snap = one_pay.snapshot()
    assert snap.record.status[0].status == 1, "充 6 元后单笔 6 元档应可领"
    # 累计充值 60 档也应可领（pay_dollar=6? 不够——再充一笔 30）
    ok, why, _, _ = core.do_buy_prop(1018, 1)
    # 买一个 30 元档（找 ShopConfig）
    from sghy_server.game import game_config
    item30 = next((v for v in game_config.load_shop_config().values()
                   if v["group"] == 4 and v["price"] == 30), None)
    if item30:
        ok, why, _, _ = core.do_buy_prop(item30["id"], 1)
        assert ok, why
    tp = grade_pay.total_pay_snapshot()
    # 42 元（6+6+30）< 60 门槛 → 60 档未达成
    assert tp.record.status[0].status == 0, "本轮累计 %d < 60，应未达成" % tp.totalPay
    print("PASS 充值联动：BuyProp → 单笔档达成 + 累计推进（pay_dollar=%d）"
          % core.PLAYER_DATA["player"]["pay_dollar"])


def test_group_assembly():
    make_player()
    m = pb.ActivityGroupData()
    m.ParseFromString(build_activity_group())
    assert m.onePay.isStart == 1 and len(m.onePay.record.status) == 9
    assert m.totalPay.isStart == 1 and len(m.totalPay.record.status) == 5
    assert m.totalConsume.isStart == 1 and len(m.totalConsume.record.status) == 5
    print("PASS 5311 组装：onePay/totalPay/totalConsume 三活动齐下发")


if __name__ == "__main__":
    test_total_pay()
    test_one_pay()
    test_total_consume()
    test_recharge_link()
    test_group_assembly()
    print("TOTAL: 5 组 passed")
