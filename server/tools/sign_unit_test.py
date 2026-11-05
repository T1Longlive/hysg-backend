"""签到功能单元测试（2026-10-22）。

规则（用户定义）：上线一天签一天、奖励按序领取；漏签可用元宝补签（补签=领下一天
奖励）；每月刷新进度；部分奖励按 VIP 双倍领取。
验证：首签推进/VIP 双倍/重复拒绝/动态漏签数/补签递增价/月度刷新/CharacterData 5 字段。

用法：python tools/sign_unit_test.py
"""

import sys
import os
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_sign_unit_test_player.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core, game_config
from sghy_server.proto import ps_protobuf_pb2 as pb


def make_player(vip=8, diamond=10000, sign=None):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 999999, "lv": 60, "vip": vip, "diamond": diamond},
        "heroes": {}, "equips": {}, "items": {},
    })
    if sign:
        core.PLAYER_DATA["sign"] = dict(sign)
    return core.PLAYER_DATA


def test_config_and_month_slice():
    awards = game_config.get_month_sign_awards(2026, 10)
    assert awards, "2026-10 应有签到奖励"
    assert all(20261001 <= a["id"] <= 20261031 for a in awards), "切片应都在当月"
    ids = [a["id"] for a in awards]
    assert ids == sorted(ids), "应按日期升序"
    assert any("awards" in a and a["awards"] for a in awards), "奖励应非空"
    print("PASS 配置加载：%d 条（2026-10），首条 id=%d vip=%d 奖励=%s"
          % (len(awards), awards[0]["id"], awards[0]["vip_lv"], awards[0]["awards"]))


def test_sign_flow():
    make_player(vip=8)
    ok, why, awards = core.do_sign()
    assert ok, "首签应成功: %s" % why
    st = core._sign_state()
    # VIP 达标带 VIPLv 的奖励走双倍路径（today=2），否则 today=1
    expect_today = 2 if "双倍" in why else 1
    assert st["sign_id"] == 1 and st["today_sign_times"] == expect_today and st["sign_times"] == 1, st
    print("PASS 首签：sign_id=1 today=%d（%s），奖励 %s" % (expect_today, why, awards))

    # VIP 双倍：首签直接 2×（弹窗数量双倍）
    today_awards = core._sign_awards_today()
    cur = today_awards[0]
    if cur["vip_lv"] > 0 and 8 >= cur["vip_lv"] and cur["extra_times"] >= 1:
        # 已在 test_sign_flow 首签走双倍分支——重置后再验一次数量
        make_player(vip=8)
        ok, why, got = core.do_sign()
        assert ok and "双倍" in why, "VIP 达标首签应双倍: %s" % why
        for g, base in zip(got, cur["awards"]):
            bv = base.get("value") if base.get("kind") == "property" else base.get("count")
            gv = g.get("value") if g.get("kind") == "property" else g.get("count")
            assert gv == bv * 2, "双倍数量应 ×2：%s vs 基础 %s" % (gv, bv)
        assert core._sign_state()["today_sign_times"] == 2
        print("PASS VIP 双倍：首签弹窗数量 ×2（%s），today=2" % got)
    else:
        print("SKIP 双倍（今天这条 VIPLv=%d 无双倍）" % cur["vip_lv"])

    ok, why, _ = core.do_sign()
    assert not ok, "应拒绝重复签到: %s" % why
    print("PASS 重复签到拒绝：%s" % why)


def test_miss_and_buy():
    # 构造：本月已过 N-1 天但进度 0 → 漏签 N-1
    import datetime
    d = datetime.date.today().day
    make_player(vip=5, diamond=1000, sign={"month": "%04d%02d" % (datetime.date.today().year,
                                                                  datetime.date.today().month),
                                          "sign_id": 0, "today_sign_times": 0,
                                          "bug_sign_times": 0, "last_day": ""})
    st = core._sign_state()
    assert st["miss_sign_times"] == max(0, d - 1), "漏签=%d 期望 %d" % (st["miss_sign_times"], max(0, d - 1))

    ok, why, awards = core.do_buy_sign()
    if d <= 1:
        print("SKIP 补签（今天是月初无漏签）")
        return
    assert ok, "补签应成功: %s" % why
    st = core._sign_state()
    assert st["sign_id"] == 1 and st["bug_sign_times"] == 1
    assert core.PLAYER_DATA["player"]["diamond"] == 1000 - 20, "首次补签应扣 20 元宝"
    print("PASS 补签：sign_id=1 bug=1 扣 20 元宝，奖励 %s" % awards)

    # 第二次补签价格 40（月初只有 1 天漏签，2 号当天补完就没有可补天数了）
    if d <= 2:
        print("SKIP 第二次补签（今天 %d 号，只剩 1 天漏签）" % d)
    else:
        ok, why, _ = core.do_buy_sign()
        assert ok, "第二次补签应成功: %s" % why
        assert core.PLAYER_DATA["player"]["diamond"] == 1000 - 20 - 40, "第二次应扣 40"
        print("PASS 补签递增价：第二次扣 40")

    # VIP 不足
    make_player(vip=4, sign={"month": "%04d%02d" % (datetime.date.today().year,
                                                    datetime.date.today().month),
                             "sign_id": 0, "today_sign_times": 0, "bug_sign_times": 0,
                             "last_day": ""})
    ok, why, _ = core.do_buy_sign()
    assert not ok and "VIP" in why, "VIP4 应拒绝补签: %s" % why
    print("PASS VIP 门槛：%s" % why)


def test_full_month_claims():
    """用户实测场景（2026-10-22）：今天 1 免费签 + 21 次补签 = 22 次恰好覆盖 1..22 号。
    修正前少算今天一格（1+20=21 封顶）。"""
    import datetime
    d = datetime.date.today().day
    ym = "%04d%02d" % (datetime.date.today().year, datetime.date.today().month)
    make_player(vip=8, diamond=999999,
                sign={"month": ym, "sign_id": 0, "today_sign_times": 0,
                      "bug_sign_times": 0, "last_day": ""})
    ok, _, _ = core.do_sign()
    assert ok
    n = 1
    while True:
        st = core._sign_state()
        if st["miss_sign_times"] <= 0:
            break
        ok, why, _ = core.do_buy_sign()
        assert ok, "补签应继续成功直到覆盖今天: %s" % why
        n += 1
        assert n <= 31, "异常循环"
    assert core._sign_state()["sign_id"] == d, "应恰好领到今天(%d 天)" % d
    assert n == d, "总领取次数应=%d，实际 %d" % (d, n)
    ok, why, _ = core.do_buy_sign()
    assert not ok and ("无天可补" in why or "已领到" in why or "没有漏签" in why), why
    print("PASS 满额领取：1 签 + %d 补 = %d 次覆盖到今天，再补被拒（%s）" % (d - 1, n, why))


def test_month_reset():
    make_player(sign={"month": "202609", "sign_id": 25, "today_sign_times": 1,
                      "bug_sign_times": 3, "sign_times": 100, "last_day": "20260930"})
    st = core._sign_state()
    assert st["sign_id"] == 0 and st["bug_sign_times"] == 0, "跨月应清零进度: %s" % st
    # 下发口径已改为“当月进度”（客户端按月刷新格子），累计值仍留在原始存档里
    assert st["sign_times"] == 0, "跨月下发应为当月进度 0: %s" % st
    assert core.PLAYER_DATA["sign"]["sign_times"] == 100, "累计次数应保留"
    print("PASS 月度刷新：进度/补签清零，累计保留")


def test_character_fields():
    make_player()
    core.do_sign()
    m = pb.CharacterData()
    m.ParseFromString(core.update_character_body())
    st = core._sign_state()
    assert m.signTimes == st["sign_times"], m.signTimes
    assert m.signId == st["sign_id"], m.signId
    assert m.todaySignTimes == st["today_sign_times"], m.todaySignTimes
    assert m.missSignTimes == st["miss_sign_times"], m.missSignTimes
    assert m.bugSignTimes == st["bug_sign_times"], m.bugSignTimes
    print("PASS CharacterData 签到 5 字段（11/12/13/21/22）一致")


def test_pay_fields_vip_progress():
    """VIP 进度条（PayPanel）：cur = payDollar - VIP[档].Price 必须非负，
    否则 setScaleX(负数) 进度条水平反向（2026-10-22 用户报）。"""
    from sghy_server.game import game_config
    make_player(vip=8)
    m = pb.CharacterData()
    m.ParseFromString(core.update_character_body())
    price = game_config.get_vip_price(8)
    assert price == 6000, "VIP8 门槛应 6000，实际 %d" % price
    assert m.payDollar >= price,         "payDollar(%d) 应 >= VIP8 门槛(%d)，否则进度条反向" % (m.payDollar, price)
    print("PASS VIP 进度条字段：payDollar=%d >= 门槛 %d（cur=%d 非负）"
          % (m.payDollar, price, m.payDollar - price))


def test_award_result_popup():
    """奖励弹窗帧：8000+AwardResult（type=0 属性 / type=物品id 道具），值正确。"""
    from sghy_server.game import builders_pb
    body = builders_pb.build_award_result("Sign", [(0, 30, "Dollar"), (20002, 10)])
    m = pb.AwardResult()
    m.ParseFromString(body)
    assert m.type == "Sign"
    assert len(m.datas) == 2
    assert m.datas[0].type == 0 and m.datas[0].name == "Dollar" and m.datas[0].value == 30
    assert m.datas[1].type == 20002 and m.datas[1].value == 10
    print("PASS AwardResult 弹窗帧（属性 type=0 + 道具 type=id）")


def test_sign_persist_roundtrip():
    """签到状态必须随存档持久化（2026-10-22 用户报：重启后可从头签到）。
    验证 repo save -> load 往返后 sign 键完整保留。"""
    import asyncio, tempfile, os as _os
    from sghy_server.data.store import SaveStore

    async def _roundtrip():
        db_path = _os.path.join(tempfile.gettempdir(), "sghy_sign_persist_test.db")
        if _os.path.exists(db_path):
            _os.remove(db_path)
        st = SaveStore(db_path=db_path, player_id=1001, json_path=None)
        data = await st.start()
        data["sign"] = {"month": "202610", "sign_id": 17, "today_sign_times": 2,
                        "bug_sign_times": 3, "sign_times": 88, "last_day": "20261022"}
        data["lottery"] = {"pity": 37, "records": [1, 2]}
        data.setdefault("player", {})["lv"] = 60
        st.mark_dirty()
        await st.flush(force=True)
        # 重新 load（等价于重启）
        st2 = SaveStore(db_path=db_path, player_id=1001, json_path=None)
        data2 = await st2.start()
        await st.stop()
        await st2.stop()
        return data2

    data2 = asyncio.get_event_loop().run_until_complete(_roundtrip())
    sg = data2.get("sign")
    assert sg and sg.get("sign_id") == 17 and sg.get("sign_times") == 88,         "sign 应完整往返，实际: %s" % sg
    lt = data2.get("lottery")
    assert lt and lt.get("pity") == 37 and lt.get("records") == [1, 2], "任意顶层键应自动往返: %s" % lt
    assert "sign" not in data2.get("player", {}) and "_top" not in data2.get("player", {}), "顶层键不应混进 player"
    print("PASS 顶层键自动持久化往返：sign=88 次 / lottery pity=37 均保留")


if __name__ == "__main__":
    test_config_and_month_slice()
    test_sign_flow()
    test_miss_and_buy()
    test_full_month_claims()
    test_month_reset()
    test_character_fields()
    test_sign_persist_roundtrip()
    test_pay_fields_vip_progress()
    test_award_result_popup()
    print("TOTAL: 9 组 passed")
