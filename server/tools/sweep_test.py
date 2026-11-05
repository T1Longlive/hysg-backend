"""扫荡 / 体力 / 重置次数 / 掉落 / 每日令牌任务 回归测试（2026-10-29）。

用法： cd server && python tools/sweep_test.py
覆盖：
  1. 体力：上限、扣减、不足拒绝、惰性恢复
  2. 关卡掉落：概率池来自 LevelConfig.ShowAward，命中后按归属发放
  3. 扫荡 5032：星级/次数/体力/令牌四道门禁 + 正常路径的扣费与回包
  4. 重置 6004：VIP 门禁、元宝价格、次数上限
  5. 每日令牌任务：按 VIP 选配置、领取发放、重复领取拒绝、跨天重置
"""
import json
import os
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

_REAL = r"D:/Cocos_Project/HYSG/env/player_data.json"
_TMP = os.path.join(tempfile.gettempdir(), "sghy_sweep_test.json")
if os.path.exists(_REAL):
    shutil.copy2(_REAL, _TMP)
os.environ["SGHY_PLAYER_DATA"] = _TMP

from sghy_server.game import core, level_run, economy, daily_token, game_config  # noqa: E402
from sghy_server.proto import ps_protobuf_pb2 as pb                              # noqa: E402

TOTAL = [0, 0]


def check(cond, label):
    TOTAL[0] += 1
    if cond:
        TOTAL[1] += 1
        print("  ok   %s" % label)
    else:
        print("  FAIL %s" % label)
    return cond


class FakeSession:
    def __init__(self):
        self.sent = []
        self.addr = ("test", 0)

    def send(self, opcode, body=b""):
        self.sent.append((opcode, bytes(body or b"")))

    def log(self, *a):
        print("   [srv] " + " ".join(str(x) for x in a))

    def take(self, opcode):
        for op, body in reversed(self.sent):
            if op == opcode:
                return body
        return None


def setup_player(lid=1003):
    """构造可扫荡状态：该关 3 星、体力满、令牌 20、VIP1、元宝充足。"""
    p = core.PLAYER_DATA.setdefault("player", {})
    p["ep"] = 200
    p["ep_time"] = int(time.time())
    p["vip"] = 1
    p["diamond"] = 100000
    p["lv"] = 60
    core.PLAYER_DATA.setdefault("items", {})["20004"] = 20
    core.PLAYER_DATA.setdefault("levels", {})[str(lid)] = {
        "star": 3, "leftTimes": level_run.max_times(lid), "buyTimes": 0}
    core.PLAYER_DATA.setdefault("level_daily", {"day": "", "levels": {}})
    core.PLAYER_DATA["level_daily"]["day"] = ""
    level_run.daily_state()


def main():
    LID = 1003                      # 困难关：MaxTimes=10、Ep=6
    print("\n[0] 关卡配置")
    check(level_run.max_times(LID) == 10, "MaxTimes=%d" % level_run.max_times(LID))
    check(level_run.ep_cost(LID) > 0, "Ep=%d" % level_run.ep_cost(LID))

    print("\n[1] 体力系统")
    setup_player()
    ep0 = economy.sync()
    check(economy.ep_max() > 0, "上限=%d" % economy.ep_max())
    ok, _ = economy.spend(10)
    check(ok and economy.sync() == ep0 - 10, "扣 10 -> %d" % economy.sync())
    p = core.PLAYER_DATA["player"]
    p["ep"] = 0
    p["ep_time"] = int(time.time()) - economy.recover_seconds() * 5 - 1
    got = economy.sync()
    check(got >= 5, "过 5 个恢复周期补了 %d 点" % got)
    p["ep"] = 3
    ok, reason = economy.spend(999)
    check(not ok and "不足" in reason and economy.sync() == 3, "不足拒绝且不扣：%s" % reason)

    print("\n[2] 关卡掉落池")
    pool = level_run._roll_pool(LID)
    check(len(pool) > 0, "候选池 %d 项（来自 ShowAward）" % len(pool))
    kinds = {k for _, k in pool}
    check(kinds <= {"item", "equip", "hero", "hero_random"},
          "分类合理：%s" % ",".join(sorted(kinds)))
    # 把概率拉满，断言能掉出东西
    old_cfg = level_run.drop_cfg
    level_run.drop_cfg = lambda: {"enabled": True, "rates": {"normal": 1, "hard": 1, "boss": 1},
                                  "hero_weight": 0.15,
                                  "item_count": {"normal": [1, 1], "hard": [1, 1], "boss": [1, 1]}}
    drops = level_run.roll_drops(LID, 1)
    check(len(drops) >= 1, "概率 100%% 时掉落 %d 件：%s"
          % (len(drops), [(d["kind"], d["type"], d["count"]) for d in drops]))
    level_run.drop_cfg = old_cfg
    # 关闭掉落
    level_run.drop_cfg = lambda: {"enabled": False}
    check(level_run.roll_drops(LID, 5) == [], "enabled=false 时不掉")
    level_run.drop_cfg = old_cfg

    print("\n[3] 扫荡 5032")
    setup_player()
    # 3.1 星级不足
    core.PLAYER_DATA["levels"][str(LID)]["star"] = 1
    s = FakeSession()
    core.dispatch(s, 5032, pb.IDCountMessage(id=LID, count=1).SerializeToString())
    check(s.take(5032) is None and s.take(5002) is not None, "1 星 → 5002 拒绝")
    core.PLAYER_DATA["levels"][str(LID)]["star"] = 3

    # 3.2 次数不足
    level_run.set_left(LID, 0)
    s = FakeSession()
    core.dispatch(s, 5032, pb.IDCountMessage(id=LID, count=2).SerializeToString())
    check(s.take(5032) is None, "次数 0 → 拒绝")
    level_run.set_left(LID, 10)

    # 3.3 令牌不足
    core.PLAYER_DATA["items"]["20004"] = 1
    s = FakeSession()
    core.dispatch(s, 5032, pb.IDCountMessage(id=LID, count=3).SerializeToString())
    body5002 = s.take(5002)
    check(s.take(5032) is None and body5002 is not None, "令牌不足 → 拒绝")
    core.PLAYER_DATA["items"]["20004"] = 20

    # 3.4 体力不足
    core.PLAYER_DATA["player"]["ep"] = 1
    s = FakeSession()
    core.dispatch(s, 5032, pb.IDCountMessage(id=LID, count=2).SerializeToString())
    check(s.take(5032) is None, "体力不足 → 拒绝")
    core.PLAYER_DATA["player"]["ep"] = 200

    # 3.5 正常扫荡 3 次
    ep_before = economy.sync()
    tok_before = core.PLAYER_DATA["items"]["20004"]
    left_before = level_run.left_times(LID)
    coin_before = core.PLAYER_DATA["player"]["coin"]
    s = FakeSession()
    core.dispatch(s, 5032, pb.IDCountMessage(id=LID, count=3).SerializeToString())
    body = s.take(5032)
    check(body is not None, "回了 5032")
    from sghy_server.proto import ps_protobuf_pb2 as _pb
    lrs = _pb.LevelResults()
    lrs.ParseFromString(body or b"")
    check(len(lrs.results) == 1, "results=%d（合并成一条给 AwardContainer 读）" % len(lrs.results))
    r0 = lrs.results[0]
    check(r0.id == LID, "result.id=%d" % r0.id)
    check(r0.coin == level_run.LEVEL_COIN * 3, "铜币=%d（3 次的量）" % r0.coin)
    check(r0.teamExp == 300, "teamExp=%d" % r0.teamExp)
    check(lrs.HasField("characterChange"), "带 characterChange（队伍经验条）")
    check(economy.sync() == ep_before - level_run.ep_cost(LID) * 3,
          "体力 %d -> %d" % (ep_before, economy.sync()))
    check(core.PLAYER_DATA["items"]["20004"] == tok_before - 3,
          "令牌 %d -> %d" % (tok_before, core.PLAYER_DATA["items"]["20004"]))
    check(level_run.left_times(LID) == left_before - 3,
          "剩余次数 %d -> %d" % (left_before, level_run.left_times(LID)))
    check(core.PLAYER_DATA["player"]["coin"] > coin_before, "铜币真的加了")
    check(s.take(520) is not None, "推了 520 刷新关卡次数")

    print("\n[4] 重置次数 6004（照原版：月卡 → VIP → 次数 → 元宝）")
    setup_player()
    level_run.set_left(LID, 0)
    p = core.PLAYER_DATA["player"]
    p["vip"] = 1
    p["diamond"] = 100000
    # 4.1 没月卡 → 拒绝
    core.PLAYER_DATA["buy_props"] = {}
    s = FakeSession()
    core.dispatch(s, 6004, pb.IDMessage(id=LID).SerializeToString())
    check(s.take(5002) is not None, "无月卡 → 拒绝（原版付费门槛）")
    # 4.2 有月卡但 VIP0 → 拒绝
    core.PLAYER_DATA["buy_props"] = {"1016": {"id": 1, "type": 1016, "totalCount": 1,
                                              "endDate": 5}}
    p["vip"] = 0
    s = FakeSession()
    core.dispatch(s, 6004, pb.IDMessage(id=LID).SerializeToString())
    check(s.take(5002) is not None, "有月卡但 VIP0 → 拒绝")
    # 4.3 月卡 + VIP1 → 成功
    p["vip"] = 1
    s = FakeSession()
    core.dispatch(s, 6004, pb.IDMessage(id=LID).SerializeToString())
    b = s.take(6004)
    cnt = _pb.IDCountMessage()
    cnt.ParseFromString(b or b"")
    check(b is not None and cnt.count == level_run.daily_limit(LID),
          "月卡+VIP1 重置成功 → leftTimes=%d" % cnt.count)
    check(p["diamond"] == 100000 - level_run.reset_cost(0),
          "扣元宝 %d（余额 %d）" % (level_run.reset_cost(0), p["diamond"]))
    # 4.4 次数上限（VIP1 BuyLevelTimes=1）
    level_run.set_left(LID, 0)
    s = FakeSession()
    core.dispatch(s, 6004, pb.IDMessage(id=LID).SerializeToString())
    check(s.take(5002) is not None, "第二次重置被 VIP 上限拒绝")
    # 4.5 月卡过期（endDate=0）→ 拒绝
    core.PLAYER_DATA["buy_props"]["1016"]["endDate"] = 0
    level_run.set_left(LID, 0)
    s = FakeSession()
    core.dispatch(s, 6004, pb.IDMessage(id=LID).SerializeToString())
    check(s.take(5002) is not None, "月卡过期 → 拒绝")

    print("\n[4b] 每日次数口径：困难关 10 次 / 章节末关 5 次")
    rows = game_config.load_level_config() or {}
    d3 = [k for k, v in rows.items()
          if str(v.get("MaxTimes")) == "10"]
    d3f = [k for k, v in rows.items()
           if str(v.get("MaxTimes")) == "5" and str(v.get("IsFinalLevel")) == "1"]
    check(bool(d3) and level_run.daily_limit(d3[0]) == 10,
          "困难关 %s → %d 次" % (d3[0] if d3 else "-", level_run.daily_limit(d3[0]) if d3 else 0))
    check(bool(d3f) and level_run.daily_limit(d3f[0]) == 5,
          "章节末关 %s → %d 次" % (d3f[0] if d3f else "-",
                                   level_run.daily_limit(d3f[0]) if d3f else 0))
    check(level_run.daily_limit(1001) == 1, "普通关 1001 → 1 次（不能刷）")
    # ★ 存量存档里可能存着旧上限（改规则前写进去的 10）——读的时候必须钳到 5，
    #   否则客户端显示 10/5（用户 2026-10-29 实测反馈）
    fin = d3f[0]
    core.PLAYER_DATA["level_daily"]["levels"][str(fin)] = {"left": 10, "buy": 0}
    check(level_run.left_times(fin) == 5, "存量 left=10 被钳到 %d（显示 5/5）"
          % level_run.left_times(fin))

    print("\n[4c] 掉落：每轮必得 + 随机武将解析成真实武将")
    _pool = level_run._roll_pool(1003)
    check(_pool and _pool[0][1] == "hero_random", "1003 池是占位武将「随机武将」%s" % _pool)
    _got = []
    _all_hit = True
    for _i in range(5):
        _d = level_run.roll_drops(1003, 1)
        if not _d:
            _all_hit = False
        _got.extend(_d)
    check(_all_hit, "1003 连抽 5 轮每轮都有掉落（必得槽生效）")
    _hero_types = {x["type"] for x in _got if x["kind"] == "hero"}
    _hc = game_config.load_hero_config() or {}
    _placeholder = {t for t in _hero_types
                    if int(str((_hc.get(t) or {}).get("Group") or 0).strip('"') or 0) < 0}
    check(bool(_hero_types) and not _placeholder,
          "掉出的武将都是真实武将（%d 种，无占位）" % len(_hero_types))
    check(len(_got) >= 5, "5 轮共掉 %d 件（每轮至少 1 件）" % len(_got))

    print("\n[5] 每日扫荡令牌任务")
    p["vip"] = 3
    row = daily_token.mission_row()
    check(row is not None, "VIP3 命中任务 %s" % (row or {}).get("id"))
    gain = daily_token.token_gain_now()
    check(gain > 0, "可领令牌 %d 个" % gain)
    st = daily_token.state()
    st["day"] = ""
    st["claimed"] = False
    s = FakeSession()
    pushed = daily_token.push_new(s)
    check(pushed and s.take(501) is not None, "推了 501 新任务")
    # ★ 必须同时进 EnterGame 的 missions（客户端 initDataOnLogin 会清表，只靠 501 会丢）
    _ms, _ = core.mission_snapshot()
    check(any(int(m["id"]) == int(row["id"]) for m in _ms),
          "EnterGame.missions 含令牌任务 %s" % row["id"])
    tok0 = core.PLAYER_DATA["items"].get("20004", 0)
    ok, reason, r, awards = daily_token.claim(int(row["id"]))
    check(ok and awards and awards[0]["count"] == gain, "领取返回 %s" % (awards,))
    core._grant_sign_awards(awards)
    check(core.PLAYER_DATA["items"].get("20004", 0) == tok0 + gain,
          "令牌 %d -> %d" % (tok0, core.PLAYER_DATA["items"].get("20004", 0)))
    ok2, reason2, _, _ = daily_token.claim(int(row["id"]))
    check(not ok2, "同一天不能领第二次（%s）" % reason2)
    # 跨天
    daily_token.state()["day"] = "2000-01-01"
    check(daily_token.pending() is not None, "跨天后又能领")

    print("\n[6] 打关卡扣体力（LevelBegin 5030）")
    setup_player()
    ep0 = economy.sync()
    s = FakeSession()
    core.dispatch(s, 5030, pb.LevelBegin(id=LID).SerializeToString())
    cost = level_run.ep_cost(LID)
    check(economy.sync() == ep0 - cost, "体力 %d -> %d（扣 %d）" % (ep0, economy.sync(), cost))
    check(s.take(200) is not None, "推了 200 同步体力")
    core.PLAYER_DATA["player"]["ep"] = 0
    s = FakeSession()
    core.dispatch(s, 5030, pb.LevelBegin(id=LID).SerializeToString())
    check(s.take(5002) is not None, "体力 0 → 推 5002 提示（仍回 LevelBegin 不卡战斗）")
    check(s.take(5030) is not None, "体力不足也回了 LevelBegin（客户端才不会卡场景）")

    print("\n[7] 通关额外掉落（LevelEnd 5031）")
    setup_player()
    heroes = [h for h in core.PLAYER_DATA["heroes"].values() if int(h.get("position") or 0) > 0][:3]
    if not heroes:                       # 兜底：随便挑 3 个
        heroes = list(core.PLAYER_DATA["heroes"].values())[:3]
    enemy_hp = sum(int(e.get("hp") or 0) * float(e.get("scale", 100) or 100) / 100.0
                   for e in (game_config.get_level_enemies(1001) or []))
    msg = pb.LevelBattleRecord()
    msg.id = 1001
    msg.result = 1
    msg.star = 1
    msg.useTime = 60
    msg.totalDamage = int(enemy_hp * 2) if enemy_hp else 100000
    mg = msg.battleRecord.myGroup
    mg.id = 1001
    mg.formation = 1
    for h in heroes:
        hh = mg.heros.add()
        hh.id = int(h["id"])
        hh.type = int(h.get("type") or 0)
        hh.lv = int(h.get("lv") or 1)
        hh.position = int(h.get("position") or 1)
    level_run.drop_cfg = lambda: {"enabled": True, "rates": {"normal": 1, "hard": 1, "boss": 1},
                                  "hero_weight": 1.0,
                                  "item_count": {"normal": [1, 1], "hard": [1, 1], "boss": [1, 1]}}
    s = FakeSession()
    core.dispatch(s, 5031, msg.SerializeToString())
    body = s.take(5031)
    lr = pb.LevelResult()
    lr.ParseFromString(body or b"")
    check(body is not None, "回了 LevelResult")
    check(len(lr.props) >= 1, "LevelResult.props=%d（额外掉落）" % len(lr.props))
    level_run.drop_cfg = old_cfg

    print("\n[8] 队伍等级上限 60（扫荡经验不能顶破）")
    setup_player()
    p = core.PLAYER_DATA["player"]
    p["lv"] = 60
    p["exp"] = 0
    _o = level_run.add_team_exp(999999)
    check(_o[2] == 60 and _o[3] < 2700,
          "加 99 万经验仍停在 60 级（exp=%d < 2700）" % _o[3])
    p["lv"] = 61
    p["exp"] = 100
    core._sanitize_player_data()
    check(int(p["lv"]) == 60, "61 级存档被启动自检压回 60（exp 保留）")

    print("\nTOTAL: %d passed, %d failed" % (TOTAL[1], TOTAL[0] - TOTAL[1]))
    return 0 if TOTAL[1] == TOTAL[0] else 1


if __name__ == "__main__":
    sys.exit(main())
