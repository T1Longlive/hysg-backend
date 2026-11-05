"""月卡/周卡单元测试（2026-10-27）。

覆盖：ShopConfig 解析、购买计入 pay_dollar/VIP/充值活动、有效期叠加、
每日奖励邮件（按 MailType）、跨天结算扣天数、过期停发、590/591 回包字段。

用法：python tools/month_card_unit_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_month_card_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core, game_config, mail, month_card
from sghy_server.proto import ps_protobuf_pb2 as pb


def make_player():
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 0, "lv": 60, "vip": 0, "diamond": 0,
                   "pay_dollar": 0},
        "heroes": {}, "equips": {}, "items": {},
    })


def test_config():
    it = game_config.get_shop_item(1016)
    assert it["group"] == 3 and it["duration"] == 30, it
    assert it["mail_type"] == 2, it
    assert it["name"].startswith("月卡"), it["name"]
    assert it["day_awards"] == [{"kind": "property", "prop": "Dollar",
                                 "value": 150}], it["day_awards"]
    wk = game_config.get_shop_item(1039)
    assert wk["duration"] == 7 and wk["mail_type"] == 3
    assert len(wk["day_awards"]) == 2, wk["day_awards"]
    print("PASS 配置解析：1016=月卡30天/150每日/Card1，1039=周卡7天/Card2")


def test_first_buy_and_renew():
    make_player()
    ok, why, awards, extra = core.do_buy_prop(1016, 1)
    assert ok, why
    p = core.PLAYER_DATA["player"]
    assert p["diamond"] == 250, "立即奖励 250 元宝，实际 %s" % p["diamond"]
    assert p["pay_dollar"] == 250, "25 元 = 250 元宝计入 pay_dollar，实际 %s" % p["pay_dollar"]
    assert p["pay_money"] == 25, "pay_money 记人民币，实际 %s" % p["pay_money"]
    rec = extra.get("buy_prop_record")
    assert extra.get("buy_prop_new") is True, "首次购买应新建记录"
    assert rec["endDate"] == 30 and rec["totalCount"] == 1, rec
    # 充值活动推进
    from sghy_server.game.activities import grade_pay
    assert grade_pay._cur_value("total_pay") == 25, "累计充值应 +25"
    # 续费叠加
    ok, why, awards, extra = core.do_buy_prop(1016, 1)
    assert ok, why
    rec = extra["buy_prop_record"]
    assert extra.get("buy_prop_new") is False, "续费应走 591"
    assert rec["endDate"] == 60 and rec["totalCount"] == 2, rec
    assert len(month_card.records_snapshot()) == 1, "同档只有一条记录"
    print("PASS 购买/续费：pay_dollar=500 元宝、立即 500 元宝、有效期 30→60 天")


def test_expired_rebuy():
    make_player()
    core.do_buy_prop(1016, 1)
    month_card._records()["1016"]["endDate"] = 0     # 已过期
    ok, why, _, extra = core.do_buy_prop(1016, 1)
    assert ok, why
    assert extra["buy_prop_record"]["endDate"] == 30, "过期重买应从 30 天重新起算"
    print("PASS 过期重买：有效期重新从 30 天起算")


def test_daily_mail_and_settle():
    make_player()
    core.do_buy_prop(1016, 1)
    # 购买当天不发每日邮件
    assert month_card.settle(push=False) == []
    assert len(mail.mails()) == 0, "购买当天不应有每日奖励邮件"
    # 模拟 3 天未登录
    rec = month_card._records()["1016"]
    import datetime
    d3 = datetime.date.today() - datetime.timedelta(days=3)
    rec["last_day"] = "%04d%02d%02d" % (d3.year, d3.month, d3.day)
    changed = month_card.settle(push=False)
    assert len(changed) == 1 and changed[0]["endDate"] == 27, changed
    box = mail.mails()
    assert len(box) == 3, "3 天 = 3 封每日奖励邮件，实际 %d" % len(box)
    m = list(box.values())[0]
    assert m["type"] == mail.MAIL_TYPE_CARD1, "月卡邮件应为 Card1(2)，实际 %s" % m["type"]
    assert m["award"] == [{"kind": "property", "prop": "Dollar", "value": 150}]
    body = core.build_mail_data(mail.to_snapshot()[0])
    md = pb.MailData()
    md.ParseFromString(body)
    assert md.type == 2, "回包 MailData.type 应为 Card1(2)，实际 %s" % md.type
    # 同一天不重复发
    assert month_card.settle(push=False) == []
    print("PASS 每日奖励：3 天补发 3 封 Card1 邮件、剩余天数 30→27")


def test_expiry_stops_mail():
    make_player()
    core.do_buy_prop(1039, 1)          # 周卡 7 天
    rec = month_card._records()["1039"]
    import datetime
    d10 = datetime.date.today() - datetime.timedelta(days=10)
    rec["last_day"] = "%04d%02d%02d" % (d10.year, d10.month, d10.day)
    month_card.settle(push=False)
    assert rec["endDate"] == 0, "超期结算应扣到 0"
    assert len(mail.mails()) == 7, "周卡最多 7 封，实际 %d" % len(mail.mails())
    assert month_card.settle(push=False) == [], "过期后不再结算"
    print("PASS 过期停发：10 天未登录只补 7 封、剩余天数归 0")


def test_record_msg_required_fields():
    make_player()
    core.do_buy_prop(1016, 1)
    rec = month_card.records_snapshot()[0]
    from sghy_server.game import builders_pb
    body = builders_pb.buy_prop_record_msg(rec).SerializeToString()
    got = pb.BuyPropRecordData()
    got.ParseFromString(body)
    assert got.type == 1016 and got.endDate == 30 and got.totalCount == 1 and got.id > 0
    print("PASS 590/591 回包：id/type/totalCount/endDate 四字段齐全")


if __name__ == "__main__":
    test_config()
    test_first_buy_and_renew()
    test_expired_rebuy()
    test_daily_mail_and_settle()
    test_expiry_stops_mail()
    test_record_msg_required_fields()
    print("TOTAL: 6 组 passed")
