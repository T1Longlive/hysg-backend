"""武将卡/定缘卡「实例模型」单元测试（2026-10-27）。

原版口径（客户端 ItemData.lua：canStack = Unit > 1）：
  · 卡 ItemConfig Unit=1 → 不可堆叠 → 服务端每张卡一个唯一实例 id、count=1
  · 客户端 getRDCardList 不做数量展开、GoodCell 对 canStack=false 不显示数量
    → 一条 count=N 的堆叠卡在界面上只会显示 1 格且看不到 N
    （2026-10-27 报障：10 张 5 星神将卡只显示拥有卡 1）

因此 SubType=1/7 且 Unit<=1 的**全部**卡（含 HeroType=-1 的开包型武将卡
  29966 五星神将卡 / 20096 魏国卫军卡 / 24007 五星武将精选卡）都按实例入库。

覆盖：配置判定 / 旧堆叠迁移 / 发放建实例 / EnterGame 下发 / 配缘按实例消耗 /
      开包卡与选将卡按实例消耗（5008）。

用法：python tools/item_cards_unit_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_item_cards_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core, game_config, growth, builders_pb
from sghy_server.proto import ps_protobuf_pb2 as pb

CARD = 25009          # 曹仁定缘卡（4★，Group=1 缘分材料）
OPEN_CARD = 29966     # 五星神将卡（HeroType=-1 的开包型武将卡，Unit=1）
PICK_CARD = 24007     # 五星武将精选卡（SelectHero，HeroType=-1，Unit=1）


def make_player(coin=10000000):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": coin, "diamond": 0, "pay_dollar": 0},
        "heroes": {}, "equips": {}, "items": {},
    })


class _NoSend(object):
    def send(self, *a, **kw):
        raise AssertionError("卡类道具不应按 type 推 451/499")


class _FakeSession(object):
    def __init__(self):
        self.sent = []

    def send(self, op, body=""):
        self.sent.append((op, body))

    def log(self, *a, **kw):
        pass

    def ops(self):
        return [op for op, _ in self.sent]


def test_config():
    assert game_config.is_card_item(CARD), "25009 应是卡类（SubType=1/Unit=1）"
    assert game_config.is_card_item(25000), "25000 应是卡类"
    assert game_config.is_card_item(OPEN_CARD), "29966 五星神将卡 Unit=1 → 必须是卡"
    assert game_config.is_card_item(20096), "20096 魏国卫军卡 Unit=1 → 必须是卡"
    assert game_config.is_card_item(PICK_CARD), "24007 五星武将精选卡 Unit=1 → 必须是卡"
    assert game_config.is_card_item(29962), "29962 定缘替身卡 SubType=7/Unit=1 → 卡"
    assert not game_config.is_card_item(20002), "改造石是可堆叠道具"
    assert not game_config.is_card_item(20020), "跑商地图 Unit=9999 可堆叠"
    print("PASS 配置判定：SubType=1/7 且 Unit<=1 全部实例化、普通道具仍堆叠")


def test_migrate_stacks():
    make_player()
    core.PLAYER_DATA["items"] = {str(CARD): 3, str(OPEN_CARD): 10, "20002": 5}
    core.item_cards()["9000000"] = 20002        # 历史异常：普通道具被塞进实例表
    moved, restored, dropped = core.migrate_card_items()
    assert (moved, restored, dropped) == (13, 1, 0), (moved, restored, dropped)
    assert str(CARD) not in core.PLAYER_DATA["items"], "堆叠卡应被迁走"
    assert str(OPEN_CARD) not in core.PLAYER_DATA["items"], "开包卡也应迁走"
    assert core.PLAYER_DATA["items"]["20002"] == 6, "普通道具不受影响（并回 1 个）"
    cards = core.item_cards()
    assert len(cards) == 13, "3 张定缘卡 + 10 张神将卡 = 13 个实例"
    assert sum(1 for v in cards.values() if int(v) == OPEN_CARD) == 10
    assert len(set(cards.keys())) == 13, "每张卡必须是唯一实例 id"
    print("PASS 迁移：3+10 张堆叠卡 → 13 个唯一实例；非卡实例并回堆叠")


def test_add_item_instances():
    make_player()
    created = core.add_item(CARD, 2)
    assert len(created) == 2 and len(set(created)) == 2
    assert all(core.card_type_of(u) == CARD for u in created)
    assert core.item_count(CARD) == 2
    # 普通道具仍堆叠、返回值空
    assert core.add_item(20002, 7) == []
    assert core.item_count(20002) == 7
    # 按 type 推数量对卡类是无意义的（不发 451/499）
    assert core.push_item_count(_NoSend(), CARD) == 2
    print("PASS 发放：卡类逐张建实例、普通道具堆叠、不误推 451/499")


def test_grant_sign_awards():
    make_player()
    core._grant_sign_awards([{"kind": "object", "type": CARD, "count": 3}])
    assert core.item_count(CARD) == 3
    assert len(core.item_cards()) == 3
    print("PASS 通用发奖：_grant_sign_awards 按实例入库")


def test_entergame_items():
    make_player()
    uids = core.add_item(OPEN_CARD, 10)
    core.add_item(25000, 1)
    core.add_item(20002, 5)
    body = builders_pb.build_enter_game()
    e = pb.EnterGame()
    e.ParseFromString(body)
    got_cards = sorted((i.id, i.type, i.count) for i in e.items if i.type == OPEN_CARD)
    assert len(got_cards) == 10, "EnterGame 应下发 10 条神将卡实例，实际 %r" % (got_cards,)
    assert {g[0] for g in got_cards} == set(uids), "实例 id 必须唯一"
    assert all(g[2] == 1 for g in got_cards), "每张卡 count=1"
    assert any(i.id == 20002 and i.type == 20002 and i.count == 5 for i in e.items)
    print("PASS EnterGame：10 张同名卡 = 10 条 ItemData（唯一 id/count=1）")


def test_open_gift_card_consume():
    """5008 SendAward：开包卡实例 uid → 用掉一张、推 499 删格、剩 1 张。"""
    make_player()
    uid_a, uid_b = core.add_item(OPEN_CARD, 2)
    sess = _FakeSession()
    ok, why = core.do_use_gift(sess, uid_a, 1)
    assert ok, why
    assert core.card_type_of(uid_a) is None, "被使用的神将卡实例应删除"
    assert core.card_type_of(uid_b) == OPEN_CARD, "另一张同名卡必须保留"
    assert 499 in sess.ops(), "应推 DeleteProp(499) 让该卡格消失"
    assert 301 in sess.ops(), "应推开包结果弹窗(301)"
    print("PASS 开包卡：按实例消耗 1 张、推 499 删格、同名卡保留")


def test_select_hero_card_consume():
    """5008 SelectHero：选将卡实例 uid → 404 新武将 + 499 删格。"""
    make_player()
    uid = core.add_item(PICK_CARD, 1)[0]
    sess = _FakeSession()
    pick = (game_config.get_item(PICK_CARD) or {}).get("heros")[0]
    ok, why = core.do_use_select_hero(sess, uid, 1, pick)
    assert ok, why
    assert core.card_type_of(uid) is None
    assert 404 in sess.ops() and 499 in sess.ops()
    print("PASS 选将卡：按实例消耗并推 404/499")


def _find_owner_and_card():
    """找一个能吃掉 CARD 的武将实例（其缘分条件目标 Group == 卡的 Group）。"""
    target_group, _star = growth.fate_item_info(CARD)
    for row in (growth._hero_cfg or {}).values():
        try:
            htype = int(growth._num(row.get("ID")))
        except (TypeError, ValueError):
            continue
        for fid in growth.hero_fate_ids(htype):
            if growth.fate_kind(fid) == 1:
                continue
            if target_group in growth.fate_targets(fid):
                hid = core._create_hero_instance(htype)
                hero = core._find_hero(hid)
                if hero:
                    return hero
    return None


def test_fasten_fate_consume_instance():
    make_player()
    owner = _find_owner_and_card()
    assert owner, "没找到可配缘的武将/材料组合"
    uid_a, uid_b = core.add_item(CARD, 2)
    before_coin = int(core.PLAYER_DATA["player"]["coin"])
    ok, why, touched, _ = core.do_fasten_fate(owner, uid_a)
    assert ok, why
    assert core.card_type_of(uid_a) is None, "被使用的卡实例应删除"
    assert core.card_type_of(uid_b) == CARD, "另一张同名卡必须保留"
    cost = core.fate_coin_cost(growth.fate_item_info(CARD)[1])
    assert before_coin - int(core.PLAYER_DATA["player"]["coin"]) == cost, "铜币按星级扣"
    # 同一实例重复使用应被拒（客户端与服务端不同步保护）
    ok2, why2, _t, _ = core.do_fasten_fate(owner, uid_a)
    assert not ok2, "已消耗的实例不能再用"
    print("PASS 配缘：按实例消耗 1 张、同名卡保留、铜币按星级扣")


if __name__ == "__main__":
    test_config()
    test_migrate_stacks()
    test_add_item_instances()
    test_grant_sign_awards()
    test_entergame_items()
    test_open_gift_card_consume()
    test_select_hero_card_consume()
    test_fasten_fate_consume_instance()
    print("TOTAL: 8 组 passed")
