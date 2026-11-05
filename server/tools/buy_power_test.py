"""元宝购买体力/铜钱 单测（2026-10-24）。

覆盖：免费次数优先（0 元宝）、VIP 次数上限、价格按次数递增、体力/铜钱到账、
铜钱暴击（倍率 + 推送消息）、次数用尽拒绝、元宝不足拒绝、actionTime 下发、跨天重置。
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(), "sghy_buy_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sghy_server.game import core, buy, growth
from sghy_server.proto import ps_protobuf_pb2 as pb


class FixedRng:
    def __init__(self, v):
        self.v = v

    def random(self):
        return self.v


def reset(vip=0, diamond=10000, lv=60):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"lv": lv, "vip": vip, "diamond": diamond, "coin": 0, "ep": 0},
        "heroes": {}, "equips": {}, "items": {},
    })
    buy.state()["day"] = "1999-01-01"      # 触发一次重置


def test_vip_limits():
    assert buy._vip_limit(0, "BuyEpTimes") == 1
    assert buy._vip_limit(0, "BuyCoinTimes") == 6
    assert buy._vip_limit(9, "BuyEpTimes") == 10
    assert buy._vip_limit(15, "BuyEpTimes") >= 10
    print("PASS VIP 次数上限：VIP0 体力1/铜钱6，VIP9 体力10（按 Lv 字段匹配，不是 ID）")


def test_free_first():
    reset()
    buy.add_free_ep(2)
    ok, reason, cost, gain, is_free = buy.buy_ep()
    assert ok and cost == 0 and gain == 50 and is_free, (ok, cost, gain, is_free)
    assert core.PLAYER_DATA["player"]["ep"] == 50
    assert core.PLAYER_DATA["player"]["diamond"] == 10000, "免费不该扣元宝"
    assert buy.snapshot()["freeBuyEpTimes"] == 1, buy.snapshot()
    assert buy.snapshot()["buyEpTimes"] == 0, "免费不占 VIP 次数"

    ok, reason, cost, gain, is_free = buy.buy_ep()
    assert ok and cost == 0 and is_free
    ok, reason, cost, gain, is_free = buy.buy_ep()
    assert ok and cost == 25 and not is_free, (cost, is_free)
    assert core.PLAYER_DATA["player"]["diamond"] == 9975
    print("PASS 免费优先：2 次免费 0 元宝、不占次数，之后按 25 元宝计费")


def test_vip_cap_and_price():
    reset(vip=0)
    ok, reason, cost, gain, is_free = buy.buy_ep()
    assert ok and cost == 25
    ok, reason, _, _, _ = buy.buy_ep()
    assert not ok and "次数已用完" in reason, reason
    assert core.PLAYER_DATA["player"]["diamond"] == 9975, "被拒不能扣钱"

    reset(vip=8)
    assert buy._price(0, "BuyEp") == 25 and buy._price(1, "BuyEp") == 25
    prices = [buy._price(i, "BuyEp") for i in range(3)]
    assert prices == [25, 25, 50], prices
    print("PASS VIP 上限拦截 + 价格按次数递增（%s）" % prices)


def test_no_diamond():
    reset(diamond=10)
    ok, reason, cost, gain, is_free = buy.buy_ep()
    assert not ok and "元宝不足" in reason, reason
    assert core.PLAYER_DATA["player"]["ep"] == 0
    print("PASS 元宝不足拒绝：%s" % reason)


def test_buy_coin_crit():
    reset(vip=0, diamond=10000, lv=60)
    row = growth._row_at(60)
    base = int(growth._num(row.get("LevelBuyCoin")))
    assert base > 0

    ok, reason, cost, gained, is_free, crit = buy.buy_coin(rng=FixedRng(0.0))
    assert ok and cost == 5, (cost, reason)
    assert crit and crit["rate"] == 2 and crit["coin"] == base
    assert crit["rateCoin"] == base * 2 == gained
    assert core.PLAYER_DATA["player"]["coin"] == base * 2, core.PLAYER_DATA["player"]["coin"]

    ok, reason, cost, gained, is_free, crit = buy.buy_coin(rng=FixedRng(0.99))
    assert ok and crit is None and gained == base
    assert core.PLAYER_DATA["player"]["coin"] == base * 3
    print("PASS 买铜钱：基础 %d / 暴击 %d（×2）/ 不暴击 %d" % (base, base * 2, base))


def test_coin_crit_push_always():
    """★ 每次买铜钱都要推 10053（非暴击 rate=1）—— 否则玩家看不到获得多少。"""
    reset(vip=0, diamond=10000, lv=60)

    class Sess:
        def __init__(self):
            self.sent = []

        def send(self, op, body):
            self.sent.append((op, body))

        def log(self, *a):
            pass

    sess = Sess()
    core.dispatch(sess, core.OP_BUY_COIN, b"")
    crits = [b for op, b in sess.sent if op == core.OP_COIN_CRIT]
    assert len(crits) == 1, "非暴击也必须推 10053，实际 %d 条" % len(crits)
    info = pb.CoinCritRateInfo()
    info.ParseFromString(crits[0])
    # dispatch 里走真随机，所以 rate 可能是 1（普通）或 2（暴击），两者都必须推
    assert info.rate in (1, 2), info.rate
    assert info.rateCoin == info.coin * info.rate, (info.coin, info.rate, info.rateCoin)
    assert info.coin == 34000, info.coin
    assert core.OP_BUY_COIN in [op for op, _ in sess.sent]
    print("PASS 每次买铜钱都推 10053：rate=%d 获得 %d（基础 %d）" % (info.rate, info.rateCoin, info.coin))


def test_coin_exhausted():
    reset(vip=0)
    limit = buy._vip_limit(0, "BuyCoinTimes")
    for _ in range(limit):
        ok, reason, *_ = buy.buy_coin(rng=FixedRng(0.99))
        assert ok, reason
    ok, reason, *_ = buy.buy_coin(rng=FixedRng(0.99))
    assert not ok and "次数已用完" in reason, reason
    print("PASS 铜钱次数用尽（VIP0 上限 %d 次）后拒绝" % limit)


def test_action_time_payload():
    reset()
    buy.add_free_ep(1)
    buy.add_free_coin(2)
    ok, *_ = buy.buy_ep()          # 用掉一次免费
    assert ok
    m = pb.CharacterData()
    m.ParseFromString(core.build_character_data())
    at = m.actionTime
    assert at.buyEpTimes == 0, at.buyEpTimes
    assert at.buyCoinTimes == 0, at.buyCoinTimes
    assert at.FreeBuyEpTimes == 0, at.FreeBuyEpTimes
    assert at.FreeBuyCoinTimes == 2, at.FreeBuyCoinTimes
    # 其余字段保持 proto default -1（客户端 `0 <= x` 判断会跳过）
    # ★ 例外：leftTherionTimes 自 2026-10-29 起真实下发（挑战面板剩余次数），
    #   不能是 -1，否则挑战面板次数永远不刷新。见 game/challenge.py。
    from sghy_server.game import challenge as _ch
    assert at.leftTherionTimes == _ch.therion_left(), at.leftTherionTimes
    print("PASS actionTime 下发：%s（leftTherionTimes=%d 挑战次数，其余 -1 客户端跳过）" % ({
        "buyEpTimes": at.buyEpTimes, "buyCoinTimes": at.buyCoinTimes,
        "freeEp": at.FreeBuyEpTimes, "freeCoin": at.FreeBuyCoinTimes},
        at.leftTherionTimes))


def test_daily_reset():
    reset()
    buy.add_free_ep(3)
    buy.buy_ep()
    assert buy.snapshot()["freeBuyEpTimes"] == 2
    buy.state()["day"] = "1999-01-01"     # 模拟跨天
    snap = buy.snapshot()
    assert snap["freeBuyEpTimes"] == 0 and snap["buyEpTimes"] == 0, snap
    print("PASS 跨天重置：四个计数清零")


if __name__ == "__main__":
    test_vip_limits()
    test_free_first()
    test_vip_cap_and_price()
    test_no_diamond()
    test_buy_coin_crit()
    test_coin_crit_push_always()
    test_coin_exhausted()
    test_action_time_payload()
    test_daily_reset()
    print("\n全部通过 ✔")
