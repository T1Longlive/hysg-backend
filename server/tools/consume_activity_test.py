"""累计消耗活动测试（2026-10-23）：余额差值法。

规则（用户拍板）：记录**元宝实时消耗**；余额减少即计入（不管哪个渠道花的，
永不遗漏）；充值等余额上升不计；每轮（默认 7 天）开始清零。

用法：python tools/consume_activity_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_consume_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.game.activities import grade_pay


def make_player(diamond=10000):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 1000000, "lv": 60, "vip": 8, "diamond": diamond, "pay_dollar": 0},
        "heroes": {}, "equips": {}, "items": {},
    })


def test_balance_diff_catches_all():
    make_player(10000)
    grade_pay.sync_dollar_consume()      # 首次：建立基准 10000
    assert grade_pay._cur_value("total_consume") == 0
    # 任意渠道扣 3000（直接改余额，模拟某个未知扣点——余额法也应捕获）
    core.PLAYER_DATA["player"]["diamond"] = 7000
    assert grade_pay._cur_value("total_consume") == 3000, "余额减少 3000 应计入"
    # 再扣 2000
    core.PLAYER_DATA["player"]["diamond"] = 5000
    assert grade_pay._cur_value("total_consume") == 5000, "累计 5000"
    print("PASS 余额差值法：任意渠道扣元宝都能捕获（3000+2000=5000）")


def test_recharge_not_counted():
    make_player(10000)
    grade_pay.sync_dollar_consume()
    # 充值加元宝（余额上升）不计入
    core.PLAYER_DATA["player"]["diamond"] = 50000
    assert grade_pay._cur_value("total_consume") == 0, "余额上升不计消耗"
    # 之后花 8000
    core.PLAYER_DATA["player"]["diamond"] = 42000
    assert grade_pay._cur_value("total_consume") == 8000, "充后花 8000 计入"
    print("PASS 充值不计：余额上升不计数，后续消耗正常计")


def test_grade_unlock():
    make_player(10000)
    grade_pay.sync_dollar_consume()
    # 消耗 600 元宝
    core.PLAYER_DATA["player"]["diamond"] = 9400
    snap = grade_pay.total_consume_snapshot()
    assert snap.totalConsume == 600, "进度 600"
    g500 = next(r for r in snap.record.status if r.Grade == 500)
    g2000 = next(r for r in snap.record.status if r.Grade == 2000)
    assert g500.status == 1, "500 档应可领"
    assert g2000.status == 0, "2000 档未达成"
    # 领 500 档
    ok, why, awards = grade_pay.claim("total_consume", "consume", 500)
    assert ok, why and "500 档应可领" or True
    snap = grade_pay.total_consume_snapshot()
    assert next(r for r in snap.record.status if r.Grade == 500).status == 2
    print("PASS 档位解锁：消耗 600 → 500 档可领 → 领后已领")


def test_round_reset():
    make_player(10000)
    grade_pay.sync_dollar_consume()
    core.PLAYER_DATA["player"]["diamond"] = 5000
    assert grade_pay._cur_value("total_consume") == 5000
    # 模拟轮次切换（改 round_key 为上一轮）
    st = core.PLAYER_DATA["activity"]["total_consume"]
    st["round_key"] = -1
    grade_pay.sync_dollar_consume()
    assert grade_pay._cur_value("total_consume") == 0, "新一轮清零"
    # 新一轮消耗正常计
    core.PLAYER_DATA["player"]["diamond"] = 4000
    assert grade_pay._cur_value("total_consume") == 1000, "新轮消耗 1000"
    print("PASS 轮次重置：轮次切换清零，新轮重新计数")


if __name__ == "__main__":
    test_balance_diff_catches_all()
    test_recharge_not_counted()
    test_grade_unlock()
    test_round_reset()
    print("TOTAL: 4 组 passed")
