"""活动 M3 单元测试（2026-10-23）：连续登录。

覆盖：登录推进（首登/连续+1/断签重置）、档位三态、领奖、5311 组装。

用法：python tools/activity_m3_test.py
"""

import os
import sys
import tempfile
import datetime

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_activity_m3_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.game.activities import login_days, build_activity_group
from sghy_server.proto import ps_protobuf_pb2 as pb


def make_player():
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 999999, "lv": 60, "vip": 0, "diamond": 0},
        "heroes": {}, "equips": {}, "items": {},
    })


def test_login_progress():
    make_player()
    # 首登 → 1 天
    d = login_days.on_login()
    assert d == 1, "首登应 1 天，实际 %s" % d
    # 同日再调不推进
    assert login_days.on_login() == 1
    # 模拟昨天登录 → 今天 +1
    st = login_days._st()
    y = datetime.date.today() - datetime.timedelta(days=1)
    st["last_day"] = "%04d%02d%02d" % (y.year, y.month, y.day)
    st["days"] = 3
    d = login_days.on_login()
    assert d == 4, "连续登录应 3+1=4，实际 %s" % d
    # 断签（3 天前）→ 重置 1
    y = datetime.date.today() - datetime.timedelta(days=3)
    st["last_day"] = "%04d%02d%02d" % (y.year, y.month, y.day)
    st["days"] = 5
    d = login_days.on_login()
    assert d == 1, "断签应重置 1，实际 %s" % d
    print("PASS 登录推进：首登1 / 连续+1 / 断签重置")


def test_claim_flow():
    make_player()
    login_days.on_login()   # days=1
    snap = login_days.snapshot()
    assert snap is not None and snap.isStart == 1
    assert len(snap.record.status) == 7, "7 天档"
    assert snap.record.status[0].status == 1, "第 1 天应可领"
    assert snap.record.status[1].status == 0, "第 2 天未达成"
    # 领第 1 天
    ok, why, awards = login_days.claim(1)
    assert ok, why
    snap = login_days.snapshot()
    assert snap.record.status[0].status == 2
    # 重复拒
    ok, why, _ = login_days.claim(1)
    assert not ok and "已领取" in why, why
    # 天数不足拒
    ok, why, _ = login_days.claim(5)
    assert not ok and "不足" in why, why
    print("PASS 连续登录：7 档三态 + 领奖/重复拒/天数不足拒")


def test_group_assembly():
    make_player()
    login_days.on_login()
    m = pb.ActivityGroupData()
    m.ParseFromString(build_activity_group())
    assert m.loginDays.isStart == 1, "loginDays 应下发"
    assert len(m.loginDays.record.status) == 7
    print("PASS 5311 组装：loginDays 7 档齐下发")


if __name__ == "__main__":
    test_login_progress()
    test_claim_flow()
    test_group_assembly()
    print("TOTAL: 3 组 passed")
