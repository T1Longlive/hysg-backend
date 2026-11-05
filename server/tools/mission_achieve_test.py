"""成就/收集类任务（mission_achieve）回归测试。2026-10-29

覆盖：
  1. 快照：成就任务进入 EnterGame 列表（星星收集狂/等级礼包/大师之路/升星之路）
  2. 进度：章节星星按 LEVEL_RECORDS 汇总；等级礼包按主公等级解锁
  3. 领奖（10100）：正常领取 → 504+503；重复领 → 拒；跳组领 → 拒
  4. 未实现 target 的组不下发（明确排除）

用法： cd server && python tools/mission_achieve_test.py
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

_REAL = r"D:/Cocos_Project/HYSG/env/player_data.json"
_TMP = os.path.join(tempfile.gettempdir(), "sghy_mission_achieve_test.json")
if os.path.exists(_REAL):
    shutil.copy2(_REAL, _TMP)
os.environ["SGHY_PLAYER_DATA"] = _TMP

from sghy_server.game import core, mission_achieve, mission, builders_pb  # noqa: E402
from sghy_server.proto import ps_protobuf_pb2 as pb           # noqa: E402

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


def star_ids(chapter):
    return [r["id"] for r in mission_achieve._star_tiers(chapter)]


def set_stars(ch1, ch2):
    """构造关卡星级记录：第1章共 ch1 星、第2章共 ch2 星。"""
    core.LEVEL_RECORDS.clear()
    core.LEVEL_RECORDS["1001"] = {"star": ch1}       # 第1章
    core.LEVEL_RECORDS["1010"] = {"star": ch2}       # 第2章


def main():
    print("=" * 60)
    print("成就/收集类任务回归（含星星对角链显示规则）")
    print("=" * 60)

    print("\n[1] 快照")
    ms = mission_achieve.snapshot_missions()
    byid = {m["id"]: m for m in ms}
    check(len(ms) >= 10, "下发 %d 条成就任务" % len(ms))
    check(10306 in byid, "含 1 级礼包(id=10306)")
    rows = mission_achieve._rows()["by_id"]
    tgt = {m["id"]: rows[m["id"]]["target"] for m in ms}
    check(set(tgt.values()) <= set(mission_achieve.SUPPORTED_TARGETS),
          "只含已实现 target：%s" % sorted(set(tgt.values())))
    check(not any(rows[m["id"]]["main_index"] for m in ms), "不含主线（主线走 mission.py）")
    check(not any(rows[m["id"]]["target_id"] == 0 for m in ms if rows[m["id"]]["target"] == 13),
          "不含 tid=0 的七日任务行")

    print("\n[2] 星星对角链显示规则（用户场景复刻）")
    # 场景：玩家通关第1、2章，共 11 星（第1章 6 星 + 第2章 5 星）
    set_stars(6, 5)
    ms = mission_achieve.snapshot_missions()
    star = [m for m in ms if rows[m["id"]]["target"] == 13]
    ch1t1, ch1t2 = star_ids(1)          # 章1 两档（阈值 1、3）
    ch2t1, ch2t2 = star_ids(2)          # 章2 两档（阈值 4、8）
    check([m["id"] for m in star] == [ch1t1, ch1t2],
          "初始只显示第1章的 2 档：%s" % [(m["id"], "%d/%d" % (m["complete"], m["need"])) for m in star])
    check(ch2t1 not in byid and ch2t2 not in byid,
          "第2章任务隐藏（第1章还没领过）——即使玩家在第2章已有 5 星")

    print("\n[3] 领取第1章第1档 → 解锁第2章第1档")
    s = drive(core.OP_SUBMIT_MISSION, f_int(1, ch1t1))
    check(len(s.pick(core.OP_MISSION_SUCCESS)) == 1, "领取成功 → 504")
    check(len(s.pick(core.OP_DELETE_MISSION)) == 1, "领取成功 → 503")
    ms = mission_achieve.snapshot_missions()
    star = [m for m in ms if rows[m["id"]]["target"] == 13]
    check([m["id"] for m in star] == [ch1t2, ch2t1],
          "现在显示：第1章剩余档 + 第2章第1档：%s" % [(m["id"], "%d/%d" % (m["complete"], m["need"])) for m in star])
    _m2 = [m for m in star if m["id"] == ch2t1][0]
    check(_m2["complete"] >= _m2["need"], "第2章第1档可领（5/4）")

    print("\n[4] 第2章第2档仍需第1章第2档领取")
    s = drive(core.OP_SUBMIT_MISSION, f_int(1, ch2t2))
    check(not s.pick(core.OP_MISSION_SUCCESS), "跳着领被拒")
    s = drive(core.OP_SUBMIT_MISSION, f_int(1, ch1t2))
    check(len(s.pick(core.OP_MISSION_SUCCESS)) == 1, "第1章第2档可领")
    ms = mission_achieve.snapshot_missions()
    star = [m for m in ms if rows[m["id"]]["target"] == 13]
    check(ch2t2 in [m["id"] for m in star], "第1章第2档领取后 → 第2章第2档出现")

    print("\n[5] 等级礼包 / EnterGame 集成")
    lv = int(core.PLAYER_DATA["player"]["lv"])
    gifts = [m for m in ms if rows[m["id"]]["target"] == -1]
    check(len(gifts) <= 1, "等级礼包同屏最多 1 个（链式，%d 个）" % len(gifts))
    check(all(m["complete"] == 1 for m in gifts), "等级礼包全部已完成（lv=%d）" % lv)
    # ★ 链式规则：领掉当前档 → 永久消失，显示下一档（阈值已达的第一个未领取档）
    if gifts:
        _first = gifts[0]["id"]
        ok, _, _, _ = mission_achieve.claim(_first)
        check(ok, "领取等级礼包 %s" % _first)
        ms2 = mission_achieve.snapshot_missions()
        _ids2 = {m["id"] for m in ms2 if rows[m["id"]]["target"] == -1}
        check(_first not in _ids2, "领掉的 %s 永久消失" % _first)
        check(len(_ids2) <= 1, "同屏仍最多 1 个等级礼包")
        # 连领到清空
        for _ in range(20):
            _cur = [m for m in mission_achieve.snapshot_missions()
                    if rows[m["id"]]["target"] == -1]
            if not _cur:
                break
            mission_achieve.claim(_cur[0]["id"])
        _left = [m for m in mission_achieve.snapshot_missions()
                 if rows[m["id"]]["target"] == -1]
        check(not _left, "全部领完后等级礼包不再出现")
    hidden = [r for r in rows.values() if int(r.get("target")) == -1
              and int(r.get("trigger_value") or 0) > lv]
    if hidden:
        check(not any(int(r["id"]) in {m["id"] for m in ms} for r in hidden),
              "未达等级的礼包（%d 条）不下发" % len(hidden))
    else:
        check(True, "没有高于当前等级（%d）的礼包档位" % lv)
    core._HERO_404_SEEN.clear()
    eg = pb.EnterGame()
    eg.ParseFromString(builders_pb.build_enter_game())
    check(any(m.id == ch2t1 for m in eg.missions), "EnterGame.missions 含星星任务（共 %d 条）" % len(eg.missions))

    print("\n[6] 计数器链：万人斩/千军破/获得武将/合成/升阶")
    core.PLAYER_DATA["mission_counters"] = {}
    # 竞技场（target=2）明确不要 → 不下发
    ms = mission_achieve.snapshot_missions()
    check(not any(rows[m["id"]]["target"] == 2 for m in ms), "竞技场链（未逢敌手）不下发")
    # 万人斩：击杀 10 → 第 1 档（3）完成可领
    mission_achieve.bump_counter("kill", 10)
    ms = mission_achieve.snapshot_missions()
    _kill = [m for m in ms if rows[m["id"]]["target"] == 0]
    check(len(_kill) == 1 and _kill[0]["id"] == 10001, "万人斩链只显示当前一档（id=%s）"
          % [m["id"] for m in _kill])
    check(_kill[0]["complete"] >= _kill[0]["need"], "击杀 10 → 第 1 档（3）可领")
    # 领第 1 档 → 第 2 档出现，进度累计
    s = drive(core.OP_SUBMIT_MISSION, f_int(1, 10001))
    check(len(s.pick(core.OP_MISSION_SUCCESS)) == 1, "万人斩第 1 档领取")
    ms = mission_achieve.snapshot_missions()
    _kill = [m for m in ms if rows[m["id"]]["target"] == 0]
    check(len(_kill) == 1 and _kill[0]["id"] == 10014 and _kill[0]["complete"] >= _kill[0]["need"],
          "第 2 档出现且进度累计（%s）" % [("%d/%d" % (m["complete"], m["need"])) for m in _kill])
    # 千军破：BOSS 计数
    mission_achieve.bump_counter("boss", 3)
    ms = mission_achieve.snapshot_missions()
    _boss = [m for m in ms if rows[m["id"]]["target"] == 1]
    check(len(_boss) == 1 and _boss[0]["complete"] >= 3, "千军破第 1 档（3 次 BOSS）可领")
    # 获得武将/合成/升阶计数
    mission_achieve.bump_counter("hero_get", 5)
    mission_achieve.bump_counter("compose", 1)
    mission_achieve.bump_counter("grade", 1)
    ms = mission_achieve.snapshot_missions()
    _h = [m for m in ms if rows[m["id"]]["target"] == 9]
    _c = [m for m in ms if rows[m["id"]]["target"] == 11]
    _g = [m for m in ms if rows[m["id"]]["target"] == 16]
    # 获得武将/合成武将在配置里是**两条并行链**（阈值系列不同），各自显示当前档；
    # 第二条链首档阈值更高（7 次获得 / 3 次合成），进度 5/7、1/3 属正常未完成
    check(len(_h) == 2 and _h[0]["complete"] >= _h[0]["need"] and _h[1]["complete"] < _h[1]["need"],
          "获得武将 2 条并行链（%s）" % [("%d/%d" % (m["complete"], m["need"])) for m in _h])
    check(len(_c) == 2 and _c[0]["complete"] >= _c[0]["need"] and _c[1]["complete"] < _c[1]["need"],
          "合成武将 2 条并行链（%s）" % [("%d/%d" % (m["complete"], m["need"])) for m in _c])
    check(len(_g) == 1 and _g[0]["complete"] >= 1, "升阶武将链进度（%s）"
          % [("%d/%d" % (m["complete"], m["need"])) for m in _g])
    # _create_hero_instance 计数（圣兽材料不计）
    _before = mission_achieve.counters().get("hero_get")
    core._create_hero_instance(10155)          # 正常武将 +1
    core._create_hero_instance(14001)          # 圣兽材料 不计
    check(mission_achieve.counters().get("hero_get") == _before + 1,
          "获得武将计数：普通武将+1、圣兽材料不计")
    core.PLAYER_DATA["mission_counters"] = {}

    print("\n[7] 存档键类型回归（JSON 加载后 claimed 键变 str，不能让任务复活）")
    # 模拟"落库 → 重新登录"后的形态：键是字符串
    core.PLAYER_DATA["mission_achieve"] = {"claimed": {str(10306): 1, str(10307): 1}}
    check(mission_achieve._claimed(10306) == 1, "str 键能被 _claimed(int) 查到")
    _lv_ids = [r["id"] for r in mission_achieve._rows()["lvpack"]]
    _lv_snap = [m["id"] for m in mission_achieve.snapshot_missions() if m["id"] in _lv_ids]
    check(10306 not in _lv_snap, "已领的等级礼包不再下发（%s）" % _lv_snap)
    # 旧存档里的 int 键 → 归一化成 str
    core.PLAYER_DATA["mission_achieve"] = {"claimed": {10308: 1}}
    check(mission_achieve._claimed(10308) == 1
          and all(isinstance(k, str) for k in core.PLAYER_DATA["mission_achieve"]["claimed"]),
          "int 键自动归一化为 str")
    # 领取后写回必须是 str 键（否则下次加载又查不到 → 又能领一遍）
    core.PLAYER_DATA["mission_achieve"] = {"claimed": {}}
    core.PLAYER_DATA["player"]["lv"] = 60
    _cur_lv = mission_achieve._lvpack_current()          # 等级礼包是链式，只能领当前档
    _ok, _why, _row, _aw = mission_achieve.claim(_cur_lv["id"])
    check(_ok and list(core.PLAYER_DATA["mission_achieve"]["claimed"]) == [str(_cur_lv["id"])],
          "领取写回 str 键（%s）" % list(core.PLAYER_DATA["mission_achieve"]["claimed"]))
    check(mission_achieve.claim(_cur_lv["id"])[0] is False, "同档二次领取被拒")

    print("\n%s  %d/%d" % ("全部通过" if TOTAL[0] == TOTAL[1] else "有失败项", TOTAL[1], TOTAL[0]))
    return 0 if TOTAL[0] == TOTAL[1] else 1


if __name__ == "__main__":
    sys.exit(main())
