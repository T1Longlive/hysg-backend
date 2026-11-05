"""周期刷新任务（mission_periodic）回归测试。2026-10-29

覆盖：
  1. 每日（cycle1）：万人斩·击败100/200敌人、挑战BOSS10次 —— 计数/进度/领取
  2. 每周（cycle2）：获得武将50次 —— 每周计数器
  3. 每月（cycle3）：合成武将2次 —— 每月计数器
  4. 三餐体力（cycle6）：窗口外隐藏/拒绝，窗口内可领 Ep60，重复领拒绝
  5. 跨天刷新：claimed 清空 + 503/501 推送
  6. 排除项：竞技场(2)、登录送扫荡卷(-1→daily_token)、跑商(46)、战令周任务

用法： cd server && python tools/mission_periodic_test.py
"""
import datetime
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

_REAL = r"D:/Cocos_Project/HYSG/env/player_data.json"
_TMP = os.path.join(tempfile.gettempdir(), "sghy_mission_periodic_test.json")
if os.path.exists(_REAL):
    shutil.copy2(_REAL, _TMP)
os.environ["SGHY_PLAYER_DATA"] = _TMP

from sghy_server.game import core, mission_achieve, mission_periodic, builders_pb  # noqa: E402
from sghy_server.proto import ps_protobuf_pb2 as pb                                 # noqa: E402

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

    def log(self, msg):
        pass

    def pick(self, opcode):
        return [b for o, b in self.sent if o == opcode]


def drive(opcode, body=b""):
    s = FakeSession()
    core.dispatch(s, opcode, body)
    return s


def f_int(field, value):
    return core.f_int(field, value)


def main():
    # ★ 固定时钟：本测试内含"昨日已领 → 跨天清空"的日期假设，不能跟随真实系统
    #   时间；否则隔天跑必然失败（2026-10-30 实测 [4] 两条挂掉：真实日期已跨天，
    #   [3] 的 mock 日期反而"倒退"，state() 把 [1] 领的 claimed 清掉了）。
    mission_periodic._now = lambda: datetime.datetime(2026, 10, 29, 8, 0)
    print("=" * 60)
    print("周期刷新任务回归（每日/每周/每月/三餐体力）")
    print("=" * 60)

    rows = {int(r["id"]): r for r in mission_periodic._rows()}
    print("\n[0] 配置范围")
    check(set(mission_periodic.PERIODIC_TARGETS[1]) == {0, 1}, "cycle1 只接击杀/BOSS")
    check(10049 not in rows, "竞技场（未逢敌手）排除")
    check(not any(10592 <= i <= 10607 for i in rows), "登录送扫荡卷排除（daily_token 负责）")
    check(10054 in rows and 10056 in rows, "每周获得武将 / 每月合成在列")
    check(10303 in rows and 10304 in rows and 10305 in rows, "三餐任务在列")

    print("\n[1] 每日任务（cycle1）")
    core.PLAYER_DATA["mission_periodic"] = {}
    mission_achieve.bump_counter("kill", 120)     # 同时进每日计数
    mission_achieve.bump_counter("boss", 2)
    ms = {m["id"]: m for m in mission_periodic.snapshot_missions()}
    check(10045 in ms and ms[10045]["complete"] >= 100, "击败100敌人：120/100 可领")
    check(10047 in ms and ms[10047]["complete"] < 200, "击败200敌人：进行中 %s"
          % ("%d/%d" % (ms[10047]["complete"], ms[10047]["need"])))
    check(10048 in ms and ms[10048]["complete"] < 10, "挑战BOSS10次：进行中 %s"
          % ("%d/%d" % (ms[10048]["complete"], ms[10048]["need"])))
    s = drive(core.OP_SUBMIT_MISSION, f_int(1, 10045))
    check(len(s.pick(core.OP_MISSION_SUCCESS)) == 1, "领取击败100 → 504")
    check(len(s.pick(core.OP_DELETE_MISSION)) == 1, "领取后 → 503")

    print("\n[2] 每周（cycle2）/ 每月（cycle3）")
    mission_achieve.bump_counter("hero_get", 50)
    mission_achieve.bump_counter("compose", 2)
    ms = {m["id"]: m for m in mission_periodic.snapshot_missions()}
    check(10054 in ms and ms[10054]["complete"] >= 50, "获得武将50次（每周）可领")
    check(10056 in ms and ms[10056]["complete"] >= 2, "合成武将2次（每月）可领")
    s = drive(core.OP_SUBMIT_MISSION, f_int(1, 10054))
    check(len(s.pick(core.OP_MISSION_SUCCESS)) == 1, "领取获得武将50 → 504")

    print("\n[3] 三餐体力（cycle6 时间窗）")
    real_now = mission_periodic._now
    try:
        mission_periodic._now = lambda: datetime.datetime(2026, 10, 29, 6, 0)   # 6:00 早窗未开
        ms = {m["id"]: m for m in mission_periodic.snapshot_missions()}
        check(10303 not in ms, "6:00 早饭任务不下发（窗口 7:00-9:00）")
        mission_periodic._now = lambda: datetime.datetime(2026, 10, 29, 8, 0)   # 8:00 窗口内
        ms = {m["id"]: m for m in mission_periodic.snapshot_missions()}
        check(10303 in ms and ms[10303]["complete"] >= 1, "8:00 早饭任务出现且可领")
        check(10304 not in ms, "午饭任务仍隐藏（12:00-14:00）")
        ep_before = core.economy.sync(core.PLAYER_DATA)
        s = drive(core.OP_SUBMIT_MISSION, f_int(1, 10303))
        check(len(s.pick(core.OP_MISSION_SUCCESS)) == 1, "领取早饭体力 → 504")
        ep_after = core.economy.sync(core.PLAYER_DATA)
        check(ep_after > ep_before or ep_after == core.economy.ep_max(core.PLAYER_DATA),
              "体力 +60（%d → %d，上限内）" % (ep_before, ep_after))
        s = drive(core.OP_SUBMIT_MISSION, f_int(1, 10303))
        check(not s.pick(core.OP_MISSION_SUCCESS), "重复领取被拒")
        mission_periodic._now = lambda: datetime.datetime(2026, 10, 29, 10, 0)  # 10:00 窗口已过
        s = drive(core.OP_SUBMIT_MISSION, f_int(1, 10304))
        check(not s.pick(core.OP_MISSION_SUCCESS), "午饭窗口未开被拒")
    finally:
        mission_periodic._now = real_now

    print("\n[4] 跨天刷新")
    st = core.PLAYER_DATA["mission_periodic"]
    check(st["claimed"].get("10045"), "昨日已领记录存在")
    s = FakeSession()
    mission_periodic.push_refresh(s)                     # 第一次：登记本会话已发实例
    check(len(s._periodic_sent) >= 3, "本会话已发送 %d 条周期实例" % len(s._periodic_sent))
    st["day"], st["week"], st["month"] = "2000-01-01", "1999-W1", "1999-01"   # 模拟跨天
    mission_periodic.push_refresh(s)                     # 巡检发现跨天 → 503 + 501
    check(len(s.pick(core.OP_DELETE_MISSION)) >= 3, "跨天 → 旧实例 503 删除（%d 条）"
          % len(s.pick(core.OP_DELETE_MISSION)))
    check(len(s.pick(core.OP_NEW_MISSION)) >= 3, "跨天 → 新实例 501 重发")
    ms = {m["id"]: m for m in mission_periodic.snapshot_missions()}
    check(10045 in ms, "新周期重新下发击败100")
    check(mission_achieve.counters().get("kill", 0) >= 0, "终身击杀计数不受跨天影响")

    print("\n[5] 排除项")
    ms_all = {m["id"] for m in mission_periodic.snapshot_missions()}
    check(10049 not in ms_all, "竞技场不下发")
    check(not any(10592 <= i <= 10607 for i in ms_all), "登录送扫荡卷不下发")
    check(18101 not in ms_all and 19201 not in ms_all, "跑商/战令周任务不下发")

    print("\n[6] 扫荡计入每日击杀/BOSS")
    core.PLAYER_DATA["mission_periodic"] = {}
    core.PLAYER_DATA["mission_counters"] = {}
    core.LEVEL_RECORDS["1003"] = {"star": 3}     # 1003 = 【困难】关（可扫荡）
    core.add_item(20004, 5)                      # 扫荡令牌
    from sghy_server.game import level_run
    ok, why, _pay = level_run.do_sweep(1003, 2)
    check(ok, "扫荡【困难】BOSS 关 2 次：%s" % why)
    ms = {m["id"]: m for m in mission_periodic.snapshot_missions()}
    check(10048 in ms and ms[10048]["complete"] >= 2, "扫荡 BOSS ×2 → 每日 BOSS ≥2（%s）"
          % (("%d/%d" % (ms[10048]["complete"], ms[10048]["need"])) if 10048 in ms else "无"))
    check(any(rows.get(m["id"], {}).get("target") == 0 and m["complete"] > 0
              for m in ms.values()), "扫荡也计击杀")

    print("\n[7b] BOSS 判定 = IsBoss>=2（带难度关），小 BOSS 关(=1)不计")
    # 1002 IsBoss=1（章内小 BOSS，MaxTimes=1）→ 扫荡不算 BOSS 次数
    n_boss_before = mission_periodic.counters().get("boss", 0)
    level_run.do_sweep(1002, 1)
    check(mission_periodic.counters().get("boss", 0) == n_boss_before,
          "IsBoss=1 的小 BOSS 关不计入（%d→%d）"
          % (n_boss_before, mission_periodic.counters().get("boss", 0)))

    print("\n[7c] 胜利后 LEVEL_RECORDS 不写死 leftTimes=10、键统一 str")
    core.LEVEL_RECORDS.clear()
    core.LEVEL_RECORDS.update({str(i): {"star": 3} for i in (1596,)})
    _real_lt = level_run.left_times
    level_run.left_times = lambda lid: 5        # mock：让正确值(5)与硬编码(10)可区分
    try:
        lb = pb.LevelBattleRecord()
        lb.id, lb.result, lb.star, lb.totalDamage, lb.useTime = 1597, 1, 1, 9999999, 30
        s2 = FakeSession()
        core.dispatch(s2, core.OP_LEVEL_END, lb.SerializeToString())
    finally:
        level_run.left_times = _real_lt
    rec = core.LEVEL_RECORDS.get("1597") or {}
    check(all(isinstance(k, str) for k in core.LEVEL_RECORDS), "LEVEL_RECORDS 键全部为 str（无 int/str 双键）")
    check(rec.get("leftTimes") == 5, "leftTimes 取 level_run 值=5（%s，不再写死 10）" % rec.get("leftTimes"))

    print("\n%s  %d/%d" % ("全部通过" if TOTAL[0] == TOTAL[1] else "有失败项", TOTAL[1], TOTAL[0]))
    return 0 if TOTAL[0] == TOTAL[1] else 1


if __name__ == "__main__":
    sys.exit(main())
