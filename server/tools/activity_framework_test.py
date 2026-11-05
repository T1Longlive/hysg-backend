"""活动系统 M1 框架单元测试（2026-10-23）。

验证：时间窗引擎（always/固定区间/openDays）、档位状态机三态、
5311 主组装（活动开 → isStart=1 + 时间窗；关 → 字段不下发）。

用法：python tools/activity_framework_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_activity_framework_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.game.activities import common, build_activity_group
from sghy_server.proto import ps_protobuf_pb2 as pb


def make_player():
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 999999, "lv": 60, "vip": 8, "diamond": 10000},
        "heroes": {}, "equips": {}, "items": {},
    })


def test_time_window():
    make_player()
    # always
    st, en, op = common.time_window({"window": {"always": True}})
    assert op, "always 应开"
    # 固定区间：已结束
    st, en, op = common.time_window({"window": {"start": 1, "end": 2}})
    assert not op, "过期区间应关"
    # 固定区间：未来开始
    import time
    now = int(time.time())
    st, en, op = common.time_window({"window": {"start": now + 3600, "end": now + 7200}})
    assert not op, "未开始应关"
    st, en, op = common.time_window({"window": {"start": now - 10, "end": now + 3600}})
    assert op, "进行中应开"
    # openDays：注册时间=现在，30 天窗口
    make_player()
    st, en, op = common.time_window({"window": {"openDays": 30}})
    assert op, "openDays 窗口内应开"
    # openDays=0：刚过起点仍开（>=start）
    core.PLAYER_DATA["player"]["register_time"] = int(time.time()) - 100
    st, en, op = common.time_window({"window": {"openDays": 1}})
    assert op, "openDays=1 且注册 100 秒前应开"
    core.PLAYER_DATA["player"]["register_time"] = int(time.time()) - 86400 * 2
    st, en, op = common.time_window({"window": {"openDays": 1}})
    assert not op, "超过 openDays 应关"
    print("PASS 时间窗：always/固定区间/openDays 三种形态")


def test_grade_status():
    assert common.grade_status(100, 60, False) == 1, "达标未领=可领"
    assert common.grade_status(50, 60, False) == 0, "未达标=未达成"
    assert common.grade_status(100, 60, True) == 2, "已领=已领取"
    print("PASS 档位状态机：0 未达成 / 1 可领 / 2 已领")


def test_config_and_switch():
    assert common.activity_cfg("total_pay") is not None, "total_pay 应启用"
    # 造一个关闭态
    common._CONFIG_CACHE = dict(common.load_activity_config())
    common._CONFIG_CACHE["total_pay"] = {"enabled": False}
    assert common.activity_cfg("total_pay") is None, "关闭后应 None"
    common._CONFIG_CACHE = None  # 清缓存恢复
    assert common.activity_cfg("total_pay") is not None
    assert common.activity_cfg("不存在活动") is None
    print("PASS 配置开关：enabled=false → 活动关闭（isStart 不下发）")


def test_build_group():
    make_player()
    m = pb.ActivityGroupData()
    m.ParseFromString(build_activity_group())
    assert m.totalPay.isStart == 1, "total_pay 开启应 isStart=1"
    assert m.totalPay.activityTime.startTime > 0
    assert m.onePay.isStart == 1, "onePay 开启应 isStart=1"
    assert m.totalConsume.isStart == 1
    import time as _t
    assert m.totalPay.activityTime.endTime > _t.time(), "always 窗口 end 应在未来（+100年）"
    print("PASS 5311 组装：onePay/totalPay/totalConsume isStart=1 + 时间窗下发")


def test_build_group_closed():
    make_player()
    common._CONFIG_CACHE = {"total_pay": {"enabled": False}}
    m = pb.ActivityGroupData()
    m.ParseFromString(build_activity_group())
    assert m.totalPay.isStart == 0, "关闭的活动不下发（默认 0）"
    common._CONFIG_CACHE = None
    print("PASS 5311 组装：关闭活动 → isStart=0（客户端入口不显）")


if __name__ == "__main__":
    test_time_window()
    test_grade_status()
    test_config_and_switch()
    test_build_group()
    test_build_group_closed()
    print("TOTAL: 5 组 passed")
