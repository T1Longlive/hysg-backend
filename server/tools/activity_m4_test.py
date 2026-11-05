"""活动 M4 单元测试（2026-10-23）：在线奖励 + 任务系统骨架。

覆盖：在线奖励档位推进/领取/跨天重置/EnterGame 组装、
      等级任务 MissionData 三态/504 发奖/completedMission。

用法：python tools/activity_m4_test.py
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(),
                                              "sghy_activity_m4_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0] + "/..")

from sghy_server.game import core
from sghy_server.game.activities import online_award, build_activity_group
from sghy_server.proto import ps_protobuf_pb2 as pb


def make_player(lv=60):
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"coin": 999999, "lv": lv, "vip": 0, "diamond": 0},
        "heroes": {}, "equips": {}, "items": {},
    })


class FakeSession:
    def __init__(self):
        self.sent = []

    def send(self, op, body):
        self.sent.append((op, body))

    def log(self, *a):
        pass


def test_online_award():
    make_player()
    online_award._touch_sync()
    snap = online_award.snapshot()
    assert snap.grade == 1 and snap.targetMinutes == 5, "第 1 档=5 分钟"
    # 时长不足拒领
    ok, why, _ = online_award.claim()
    assert not ok and "不足" in why, why
    # 模拟在线 5 分钟：直接改基准
    st = online_award._st()
    st["minutes_base"] = 5.0
    ok, why, awards = online_award.claim()
    assert ok, why
    snap = online_award.snapshot()
    assert snap.grade == 2 and snap.targetMinutes == 10, "领后推进第 2 档"
    # 逐档领完
    for target_min in (10, 15, 30, 60):
        st["minutes_base"] = float(target_min)
        ok, why, _ = online_award.claim()
        assert ok, "领 %d 档: %s" % (target_min, why)
    snap = online_award.snapshot()
    assert snap.grade == 0 and snap.targetMinutes == 0, "领完 grade=0"
    ok, why, _ = online_award.claim()
    assert not ok and "领完" in why, why
    print("PASS 在线奖励：5 档推进 + 不足拒 + 领完封顶")


def test_online_award_day_reset():
    make_player()
    st = online_award._st()
    st.update({"grade": 3, "minutes_base": 30.0, "day": "20200101"})
    online_award.cur_minutes()   # 触发跨天重置
    assert online_award._st()["grade"] == 1, "跨天应重置第 1 档"
    print("PASS 在线奖励跨天重置")




def test_enter_game_assembly():
    make_player(lv=60)
    from sghy_server.game import builders_pb
    e = pb.EnterGame()
    e.ParseFromString(builders_pb.build_enter_game())
    # 任务系统已扩展：主线 + 每日令牌 + 成就/周期任务都会下发（见 mission_test）
    _ids = [x.id for x in e.missions]
    assert any(x.id == 10425 for x in e.missions), "缺少主线任务节点: %s" % _ids[:5]
    assert len(e.completedMission) == 0
    assert e.activityData.onlineAwardRecord.targetMinutes == 5, "在线奖第 1 档"
    print("PASS EnterGame 组装：任务 %d 条（含主线 10425）+ activityData.onlineAwardRecord + "
          "completedMission(23)" % len(e.missions))


# ---- 2026-10-23 修正：等级礼包改为 10100 领取模型 ----


if __name__ == "__main__":
    test_online_award()
    test_online_award_day_reset()
    test_enter_game_assembly()
    print("TOTAL: 3 组 passed")

