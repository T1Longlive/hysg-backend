"""主线任务系统单测（2026-10-25）。

覆盖：序列加载（147 节点去重）、初始快照、通关达成、领奖发奖、链推进、
重复领奖/未完成领奖拒绝、全部完成边界、EnterGame 下发。
"""

import os
import sys
import tempfile

os.environ["SGHY_PLAYER_DATA"] = os.path.join(tempfile.gettempdir(), "sghy_mission_test.json")
if os.path.exists(os.environ["SGHY_PLAYER_DATA"]):
    os.remove(os.environ["SGHY_PLAYER_DATA"])

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sghy_server.game import core, mission, game_config
from sghy_server.proto import ps_protobuf_pb2 as pb


def reset():
    core.PLAYER_DATA.clear()
    core.PLAYER_DATA.update({
        "player": {"lv": 60, "vip": 0, "diamond": 0, "coin": 0, "ep": 0},
        "heroes": {}, "equips": {}, "items": {},
    })
    core.PLAYER_DATA.pop("mission_main", None)


def test_sequence():
    seq = game_config.load_mission_main()
    assert len(seq) == 147, len(seq)
    mis = [r["main_index"] for r in seq]
    assert mis == list(range(1, 148)), "MainIndex 必须 1..147 连续"
    tids = [r["target_id"] for r in seq]
    assert all(tids[i] <= tids[i + 1] for i in range(len(tids) - 1)), "目标关卡必须递增"
    assert all(r["target"] == 7 and r["target_value"] == 1 for r in seq), "主线全是 Target=7/Value=1"
    lc = game_config.load_level_config()
    assert all(r["target_id"] in lc for r in seq), "目标关卡必须都存在"
    print("PASS 主线序列：147 节点 / MI 1..147 / 关卡递增 / 全部存在")


def test_initial_snapshot():
    reset()
    missions, done = mission.snapshot()
    assert done == [], done
    # mission.snapshot() 只出主线；core.mission_snapshot() 会再并入每日扫荡令牌任务
    assert len(missions) == 1 and missions[0]["id"] == 10425, missions
    assert missions[0]["complete"] == 0, missions[0]
    # EnterGame 里真的下发了（主线 + 可能一条每日令牌任务，2026-10-29）
    m = pb.EnterGame()
    m.ParseFromString(core.build_enter_game())
    _ids = [x.id for x in m.missions]
    assert 10425 in _ids, _ids
    _main = [x for x in m.missions if x.id == 10425][0]
    assert _main.needValue == 1 and _main.completeValue == 0
    assert len(m.completedMission) == 0
    # 除主线外的任务 = 每日扫荡令牌 + 成就任务（mission_achieve，2026-10-29）
    _extra = [x.id for x in m.missions if x.id != 10425]
    from sghy_server.game import daily_token, mission_achieve, mission_periodic
    _row = daily_token.mission_row()
    _tok = int(_row["id"]) if _row else 0
    _sup = mission_achieve._rows()["by_id"]
    _sup.update({int(r["id"]): r for r in mission_periodic._rows()})
    _bad = [x for x in _extra if x != _tok and x not in _sup]
    assert not _bad, (_bad,)
    print("PASS 初始状态：主线 10425 已下发（令牌 %s + 成就 %d 条），completed=0"
          % (_tok or "无", len([x for x in _extra if x != _tok])))


def test_level_win_and_claim():
    reset()
    # 打错关卡 → 不达成
    changed, _ = mission.on_level_win(1005)
    assert not changed
    # 未完成就领 → 拒绝
    ok, reason, _, _ = mission.claim(10425)
    assert not ok and "尚未完成" in reason, reason
    # 打对关卡（10425 要求 1002）
    changed, node = mission.on_level_win(1002)
    assert changed and node["id"] == 10425, (changed, node)
    missions, _ = mission.snapshot()
    assert missions[0]["complete"] == 1, missions
    # 领奖
    ok, reason, node, awards = mission.claim(10425)
    assert ok, reason
    assert core.PLAYER_DATA["player"]["diamond"] == 20, core.PLAYER_DATA["player"]["diamond"]
    assert awards == [{"kind": "property", "prop": "Dollar", "value": 20}], awards
    # 重复领 → 拒绝（现在 current 已经是下一个了）
    ok2, reason2, _, _ = mission.claim(10425)
    assert not ok2, "旧任务不该能再领"
    # 推进到下一个
    nxt = mission.current()
    assert nxt["id"] == 10426 and nxt["target_id"] == 1005, nxt
    missions, done = mission.snapshot()
    assert done == [10425], done
    assert missions[0]["id"] == 10426 and missions[0]["complete"] == 0
    print("PASS 通关达成 → 领奖 +20 元宝 → 自动切到 10426（completed=[10425]）")


def test_history_sync():
    """老存档：已通关 1002/1005 → 登录即标可领，领完自动接下一个继续可领。"""
    reset()
    core.LEVEL_RECORDS.clear()
    core.LEVEL_RECORDS.update({1001: {"star": 3}, 1002: {"star": 3}, 1005: {"star": 3}})
    changed = mission.sync_with_records(core.LEVEL_RECORDS)
    assert changed, "10425 要求 1002，历史已通关应标可领"
    ok, reason, node, _ = mission.claim(10425)
    assert ok, reason
    # 领完自动切到 10426（要求 1005），历史也通关 → 应立刻可领
    nxt = mission.current()
    assert nxt["id"] == 10426, nxt
    missions, _ = mission.snapshot()
    assert missions[0]["complete"] == 1, "下一个节点历史已通关，应直接可领"
    ok, reason, _, _ = mission.claim(10426)
    assert ok, reason
    # 10427 要求 1012，历史没打 → 不可领
    missions, _ = mission.snapshot()
    assert missions[0]["id"] == 10427 and missions[0]["complete"] == 0
    ok, reason, _, _ = mission.claim(10427)
    assert not ok and "尚未完成" in reason
    print("PASS 历史通关追溯：1002/1005 已通关 → 两个主线直接可领 → 第三关不可领")


def test_claim_push_next():
    """★ 领奖后推的 501 必须带 complete=1（下一节点历史已通关）。

    曾经的 bug：501 里写死 complete=0 → 下一关明明早通过，客户端仍显示"未解锁"。
    """
    reset()
    core.LEVEL_RECORDS.clear()
    core.LEVEL_RECORDS.update({1001: {"star": 3}, 1002: {"star": 3}, 1005: {"star": 3}})
    assert mission.sync_with_records(core.LEVEL_RECORDS)

    class Sess:
        def __init__(self):
            self.sent = []

        def send(self, op, body):
            self.sent.append((op, body))

        def log(self, *a):
            pass

    sess = Sess()
    im = pb.IDMessage()
    im.id = 10425
    core.dispatch(sess, core.OP_SUBMIT_MISSION, im.SerializeToString())
    ops = [op for op, _ in sess.sent]
    assert core.OP_MISSION_SUCCESS in ops and core.OP_DELETE_MISSION in ops, ops
    news = [pb.MissionData() for b in [b for op, b in sess.sent
            if op == core.OP_NEW_MISSION]]
    for _nm, _nb in zip(news, [b for op, b in sess.sent
                               if op == core.OP_NEW_MISSION]):
        _nm.ParseFromString(_nb)
    # 2026-10-29：10100 兜底链还会推周期任务/成就的 501，只断言主线下一节点在内
    m = next((x for x in news if x.id == 10426), None)
    assert m is not None, [x.id for x in news]
    assert m.completeValue == 1, "下一节点(1005 已通关)必须直接可领，实际 complete=%s" % m.completeValue
    assert m.needValue == 1, m.needValue
    print("PASS 领奖推送：504+503+200+501(10426, complete=1) 全套")


def test_full_chain():
    reset()
    core.LEVEL_RECORDS.clear()      # 避免上一个用例留下的通关记录干扰
    seq = game_config.load_mission_main()
    total_diamond = 0
    for i, node in enumerate(seq):
        assert mission.current()["id"] == node["id"], "第 %d 步节点不对" % (i + 1)
        changed, _ = mission.on_level_win(node["target_id"])
        assert changed, "关卡 %s 应该达成" % node["target_id"]
        ok, reason, _, awards = mission.claim(node["id"])
        assert ok, "第 %d 步领奖失败: %s" % (i + 1, reason)
        total_diamond += sum(a["value"] for a in awards if a.get("kind") == "property")
    assert mission.current() is None, "全部完成后 current 应为 None"
    ok, reason, _, _ = mission.claim(seq[-1]["id"])
    assert not ok and "全部完成" in reason, reason
    missions, done = mission.snapshot()
    assert missions == [] and len(done) == len(seq), (len(missions), len(done))
    assert core.PLAYER_DATA["player"]["diamond"] == total_diamond
    print("PASS 全链贯通：147 关依次通关+领奖，累计元宝 %d，末尾 current=None" % total_diamond)


if __name__ == "__main__":
    test_sequence()
    test_initial_snapshot()
    test_level_win_and_claim()
    test_history_sync()
    test_claim_push_next()
    test_full_chain()
    print("\n全部通过 ✔")
