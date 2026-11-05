"""单笔充值活动（OnePay，2026-10-23 计数模型重写）。

客户端权威链路：
  数据：5311 ActivityGroupData.onePay = OnePayActivityData
      {record:{status:[GradeTimesStatusData{grade, gradeValue(×10=元), checkTimes,
        usedTimes, status, awards}]}, activityTime, isStart}
  领取：C→S **5322** GetOnePayAward(IDMessage{id=grade}) → ackOnePay 解
      **GradeTimesStatusData 单档**；SelectAnyItem 分支 replacePanel("AwardShow")。
      （5312 是充值自选礼包 PayChose 的 GetSinglePayAward，回 SinglePayActivityData，
      该活动已砍掉，不归本模块管。）

规则（2026-10-27 用户拍板）：
  · 档位 6/25/30/50/68/98/198/328/648 元（9 档），98 以下 4 种奖励、其余 3 种
  · **每档 checkTimes=10**：单笔充值 **等于** 档位金额 → 该档可领次数 +1（累计 10 次）
  · 7 天轮次重置（reached/used 清零）
"""

from ...proto import ps_protobuf_pb2 as pb
from . import common
from .one_pay_grades import grades_of


def _state():
    act = common.act_state()
    st = act.get("one_pay")
    if not isinstance(st, dict):
        st = act["one_pay"] = {}
    # ★ reached/used 必须是 dict（str(档序号) → 次数）。计数模型上线前的
    #   旧存档是 list 形态（reached/claimed 列表），直接按 dict 读写会抛
    #   AttributeError: 'list' object has no attribute 'get'（2026-10-27
    #   购买元宝走 on_recharge 时实测）——旧形态一律视作无进度重建。
    for key in ("reached", "used"):
        if not isinstance(st.get(key), dict):
            st[key] = {}
    # 7 天轮次重置（2026-10-23 用户方案：永久开启 + 每轮刷新）
    if common.round_reset(st, _round_days()):
        st["reached"] = {}   # str(grade 0基) → 单笔达标次数
        st["used"] = {}      # str(grade 0基) → 已领次数
    return st


def _round_days():
    cfg = common.activity_cfg("single_pay") or {}
    return int(cfg.get("round_days") or 7)


def on_recharge(single_price):
    """单笔充值达成判定：单笔 **等于** 档位金额 → 该档可领次数 +1（累计到 checkTimes）。

    ★ 2026-10-27 用户拍板：是「等于」不是「大于」——充 648 只解锁 648 档，
      不连带解锁 6/25/30...低档（旧逻辑 >= 让每笔大额充值重复计入所有低档，
      充 3 次 648 后 9 档全部可领，与客户端单笔语义不符）。
    """
    if not common.activity_cfg("single_pay"):
        return
    st = _state()
    reached = st.setdefault("reached", {})
    price = int(single_price or 0)
    for i, g in enumerate(grades_of()):
        if price and price == int(g["dollar"]):
            reached[str(i)] = int(reached.get(str(i)) or 0) + 1
            break


def snapshot():
    """OnePayActivityData（5311 onePay 字段）；未配置返回 None。"""
    cfg = common.activity_cfg("single_pay")
    if not cfg:
        return None
    start, end, is_open = common.time_window(cfg)
    if not is_open:
        m = pb.OnePayActivityData()
        m.isStart = -1
        return m
    st = _state()
    reached = st.get("reached") or {}
    used = st.get("used") or {}
    grades = grades_of()
    m = pb.OnePayActivityData()
    at = m.activityTime
    at.startTime = int(start)
    at.endTime = int(end)
    m.isStart = 1
    for i, g in enumerate(grades):
        d = m.record.status.add()
        d.grade = i + 1
        d.gradeValue = int(g["dollar"]) * 10     # 客户端 Price = gradeValue/10 = 元
        d.checkTimes = int(g.get("checkTimes") or 10)
        r_n = int(reached.get(str(i)) or 0)
        u_n = int(used.get(str(i)) or 0)
        d.usedTimes = u_n
        can = min(r_n, d.checkTimes)
        d.status = 1 if u_n < can else 0         # 0 未达成/领完，1 可领
        for a in g["awards"]:
            spec = d.awards.add()
            _fill_spec(spec, a)
    return m


def _fill_spec(spec, a):
    if a.get("kind") == "property":
        spec.type = 0
        spec.name = a.get("prop") or ""
        spec.value = int(a.get("value") or 0)
    else:
        spec.type = int(a.get("type") or 0)
        spec.name = ""
        spec.value = int(a.get("count") or 0)


def claim(grade):
    """领奖（5312）。grade=档序号(1-based)。返回 (ok, why, awards)。"""
    cfg = common.activity_cfg("single_pay")
    if not cfg:
        return False, "活动未开启", []
    grades = grades_of()
    idx = int(grade) - 1
    if not (0 <= idx < len(grades)):
        return False, "档位不存在", []
    st = _state()
    reached = st.get("reached") or {}
    used = st.setdefault("used", {})
    r_n = int(reached.get(str(idx)) or 0)
    u_n = int(used.get(str(idx)) or 0)
    check = int(grades[idx].get("checkTimes") or 10)
    if u_n >= check:
        return False, "该档领取次数已用完", []
    if r_n <= u_n:
        return False, "该档未达成", []
    used[str(idx)] = u_n + 1
    return True, "领取成功", grades[idx]["awards"]


def build_claim_ack(grade):
    """5322 回包：**GradeTimesStatusData 单档**（客户端 ackOnePay 解这个类型）。"""
    grades = grades_of()
    st = _state()
    idx = int(grade) - 1
    m = pb.GradeTimesStatusData()
    if not (0 <= idx < len(grades)):
        m.grade = int(grade)
        m.status = 0
        return m.SerializeToString()
    g = grades[idx]
    m.grade = idx + 1
    m.gradeValue = int(g["dollar"]) * 10
    m.checkTimes = int(g.get("checkTimes") or 10)
    r_n = int((st.get("reached") or {}).get(str(idx)) or 0)
    u_n = int((st.get("used") or {}).get(str(idx)) or 0)
    m.usedTimes = u_n
    can = min(r_n, m.checkTimes)
    m.status = 1 if u_n < can else 0
    for a in g["awards"]:
        _fill_spec(m.awards.add(), a)
    return m.SerializeToString()
