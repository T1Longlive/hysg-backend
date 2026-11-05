"""概率礼包/自选箱/选将卡 功能测试（2026-10-23）。

覆盖：开箱掷骰、完整使用流程（扣道具+发奖+401/451推送+301弹窗）、
自选箱选择校验、选将卡选择校验、Condition 型（30个开一次）。

用法：python tools/gift_open_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_gift_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core, game_config
from sghy_server.proto import ps_protobuf_pb2 as pb


class Sess:
    def __init__(self):
        self.sent = []

    def send(self, op, body):
        self.sent.append((op, body))

    def log(self, *a):
        pass


def make_player(items=None):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 1000000, "lv": 60, "vip": 8, "diamond": 100000},
        "heroes": {}, "equips": {}, "items": dict(items or {}),
    })


def test_roll():
    ic = game_config.load_item_config()
    assert len(ic) == 1001, "ItemConfig 1001 条"
    # 开箱型：神将卡必出武将
    a, why = core.roll_gift_awards(20115, 5)
    assert a is not None, why
    assert all(x["kind"] == "hero" for x in a), "神将卡只出武将: %s" % a
    # 碎片礼包：出道具
    a, why = core.roll_gift_awards(29937, 3)
    assert a and all(x["kind"] == "item" for x in a), a
    # 装备卡：出装备
    a, why = core.roll_gift_awards(29971, 3)
    assert a and any(x["kind"] == "equip" for x in a), a   # 装备卡池可能混道具
    print("PASS 掷骰：神将卡→武将 / 碎片包→道具 / 装备卡→装备")


def test_use_gift():
    # 20115 超神神将卡：SubType=1/Unit=1 → 卡实例（每张唯一 uid），不走 items 堆叠
    make_player({})
    uids = core.add_item(20115, 3)
    assert len(uids) == 3, "3 张卡应生成 3 个实例"
    s = Sess()
    ok, why = core.do_use_gift(s, uids[0], 1)
    assert ok, why
    assert core.card_type_of(uids[0]) is None, "用掉的卡实例应被删除"
    assert core.item_count(20115) == 2, "其余 2 张卡实例保留"
    ops = [op for op, _ in s.sent]
    assert 301 in ops, "必须有奖励弹窗 301"
    assert 499 in ops, "卡实例消耗必须推 499 删格"
    assert len(core.PLAYER_DATA["heroes"]) == 1, "武将应入实例表"
    print("PASS 开箱流程：删卡实例(499) + 武将入库 + 301 弹窗")


def test_use_gift_not_enough():
    make_player({})
    s = Sess()
    ok, why = core.do_use_gift(s, core.CARD_UID_BASE + 99999, 1)
    assert not ok and "不足" in why, why
    print("PASS 数量不足拒绝")


def test_select_item():
    make_player({"21035": 2})
    it = game_config.get_item(21035)
    pick = int(it["items"][0])
    s = Sess()
    ok, why = core.do_use_select_item(s, 21035, 1, pick)
    assert ok, why
    assert core.PLAYER_DATA["items"]["21035"] == 1
    assert core.PLAYER_DATA["items"].get(str(pick), 0) == 1, "选中道具应入库"
    assert 301 not in [op for op, _ in s.sent], "选择型不发 301（客户端自己 replacePanel）"
    # 非法选择
    ok, why = core.do_use_select_item(s, 21035, 1, 99999)
    assert not ok and "无效" in why, why
    print("PASS 自选箱：合法选择入库 + 非法选择拒绝")


def test_select_hero():
    # mock broadcast（_grant_sign_awards 内部用全局广播推 404/560，FakeSession 收不到）
    core._test_broadcasts = []
    orig_broadcast = core.broadcast
    core.broadcast = lambda op, body: core._test_broadcasts.append((op, body))
    try:
        _test_select_hero_inner()
    finally:
        core.broadcast = orig_broadcast


def _test_select_hero_inner():
    # 24007 五星武将精选卡：SubType=1/Unit=1 → 卡实例
    make_player({})
    uids = core.add_item(24007, 1)
    it = game_config.get_item(24007)
    pick = int(it["heros"][0])
    s = Sess()
    ok, why = core.do_use_select_hero(s, uids[0], 1, pick)
    assert ok, why
    assert core.card_type_of(uids[0]) is None, "用掉的选将卡实例应被删除"
    assert core.item_count(24007) == 0
    assert len(core.PLAYER_DATA["heroes"]) == 1
    ops = [op for op, _ in s.sent]
    assert 404 in ops, "选将卡必须推 404 NewHero（在 ack 之前）"
    assert 499 in ops, "卡实例消耗必须推 499 删格"
    assert 301 not in ops, "选将卡不发 301（EmployeeResultPanel 自展示）"
    print("PASS 选将卡：404 + 499 推送、无 301 + 武将入库")


def test_no_duplicate_404():
    """开箱出武将：404 只能推一次（双 404 → 客户端 addWarriorData nil 崩）。"""
    make_player({})
    core._test_broadcasts = []
    orig_broadcast = core.broadcast
    core.broadcast = lambda op, body: core._test_broadcasts.append((op, body))
    try:
        uids = core.add_item(20115, 5)
        s = Sess()
        ok, why = core.do_use_gift(s, uids[0], 1)
        assert ok, why
        # broadcast 里的 404 只一次（_grant_sign_awards 的），session 侧不再重复
        b404 = sum(1 for op, _ in core._test_broadcasts if op == 404)
        s404 = sum(1 for op, _ in s.sent if op == 404)
        assert b404 + s404 <= 1, "404 重复：broadcast=%d session=%d" % (b404, s404)
        print("PASS 无重复 404：broadcast=%d session=%d（合计 ≤1）" % (b404, s404))
    finally:
        core.broadcast = orig_broadcast


def test_condition_gift():
    """图纸碎片：30 个开一次。"""
    make_player({"21040": 30})
    s = Sess()
    ok, why = core.do_use_gift(s, 21040, 1)
    assert ok, why
    assert core.PLAYER_DATA["items"]["21040"] == 0, "应扣 30 个"
    # 不足 30 时拒绝
    core.PLAYER_DATA["items"]["21040"] = 10
    ok, why = core.do_use_gift(s, 21040, 1)
    assert not ok and "不足" in why, why
    print("PASS Condition 型：30 个开一次 / 不足拒绝")


if __name__ == "__main__":
    test_roll()
    test_use_gift()
    test_use_gift_not_enough()
    test_select_item()
    test_select_hero()
    test_condition_gift()
    test_no_duplicate_404()
    print("TOTAL: 7 组 passed")
