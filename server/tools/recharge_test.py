"""充值链路单元测试（2026-10-23）：BuyProp(5210) + VIP 联动 + 首充翻倍。

规则（用户拍板）：充值界面点购买 → 模拟支付成功 → 加钻石 + 计入累计充值 +
VIP 联动（过门槛升级）+ 首充翻倍 + 充值活动推进。

用法：python tools/recharge_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_recharge_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core


def make_player():
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 999999, "lv": 60, "vip": 0, "diamond": 0,
                   "pay_dollar": 0},
        "heroes": {}, "equips": {}, "items": {},
    })


def test_recharge_first_pay():
    make_player()
    ok, why, awards, extra = core.do_buy_prop(1018, 1)   # 6 元 60 钻档
    assert ok, why
    p = core.PLAYER_DATA["player"]
    assert p["diamond"] == 120, "首充翻倍：60+60=120，实际 %d" % p["diamond"]
    assert p["pay_dollar"] == 60, "6 元 = 60 元宝（VIP 门槛单位）"
    assert p["pay_money"] == 6, "pay_money 记人民币"
    assert p.get("first_pay_done") == 1
    assert extra.get("first_pay") is True
    print("PASS 首充：+120 钻（翻倍）、pay_dollar=60 元宝、首充标记")


def test_recharge_second_time_no_double():
    ok, why, awards, extra = core.do_buy_prop(1018, 1)
    p = core.PLAYER_DATA["player"]
    assert p["diamond"] == 120 + 60, "二购不再翻倍：180，实际 %d" % p["diamond"]
    assert p["pay_dollar"] == 120
    assert p["pay_money"] == 12
    assert not extra.get("first_pay")
    print("PASS 二购：+60 钻（不翻倍）、累计 pay_dollar=120 元宝")


def test_vip_linkage():
    make_player()
    from sghy_server.game import game_config
    big = max((v for v in game_config.load_shop_config().values()
               if v["group"] == 4), key=lambda v: v["price"])
    while core.PLAYER_DATA["player"]["pay_dollar"] < 500:
        ok, why, awards, extra = core.do_buy_prop(big["id"], 1)
        assert ok, why
    p = core.PLAYER_DATA["player"]
    assert p["vip"] >= 3, "pay_dollar=%d 应升到 VIP3+，实际 VIP%d" % (p["pay_dollar"], p["vip"])
    print("PASS VIP 联动：累计 %d → VIP%d（门槛 500=VIP3）"
          % (p["pay_dollar"], p["vip"]))


def test_vip_no_downgrade():
    make_player()
    core.PLAYER_DATA["player"]["vip"] = 8
    core.do_buy_prop(1018, 1)
    assert core.PLAYER_DATA["player"]["vip"] == 8, "VIP 只升不降"
    print("PASS VIP 只升不降：VIP8 保持")


def test_vip_progress_unit():
    """648 元档 = 6480 元宝：VIP 进度/文案都以元宝计。"""
    make_player()
    ok, why, _awards, _extra = core.do_buy_prop(1023, 1)
    assert ok, why
    p = core.PLAYER_DATA["player"]
    assert p["pay_dollar"] == 6480, "pay_dollar 应记元宝"
    assert p["pay_money"] == 648, "pay_money 应记人民币"
    assert p["vip"] == 8, "6480 元宝 → VIP8（门槛 6000）"
    d = core.pay_display()
    assert d["payDollar"] == 6480 and d["payMoney"] == 648
    print("PASS VIP 进度单位：648 元 → 6480 元宝、payMoney=648、VIP%d" % p["vip"])


def test_pay_dollar_migration():
    """旧存档 pay_dollar 是人民币 → 启动迁移 ×10，只做一次。"""
    make_player()
    core.PLAYER_DATA["player"]["pay_dollar"] = 648
    assert core.migrate_pay_dollar_unit() is True
    p = core.PLAYER_DATA["player"]
    assert p["pay_dollar"] == 6480 and p["pay_money"] == 648
    assert p["pay_dollar_unit"] == 10 and p["vip"] == 8
    assert core.migrate_pay_dollar_unit() is False, "迁移必须幂等"
    print("PASS 旧存档迁移：648 元 → 6480 元宝、VIP8、幂等")


def test_shop_item_parse():
    from sghy_server.game import game_config
    it = game_config.get_shop_item(1018)
    assert it and it["group"] == 4 and it["price"] == 6
    assert it["awards"][0]["prop"] == "Dollar" and it["awards"][0]["value"] == 60
    assert game_config.get_vip_price(3) == 500
    assert game_config.get_vip_max_lv() >= 15
    print("PASS ShopConfig 解析：1018=6元/60钻、VIP3 门槛=500、VIP 上限=%d"
          % game_config.get_vip_max_lv())


if __name__ == "__main__":
    test_recharge_first_pay()
    test_recharge_second_time_no_double()
    test_vip_linkage()
    test_vip_no_downgrade()
    test_vip_progress_unit()
    test_pay_dollar_migration()
    test_shop_item_parse()
    print("TOTAL: 7 组 passed")
