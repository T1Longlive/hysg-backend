"""累计充值 / 累计消费活动（TotalPay / TotalConsume，2026-10-23 M2）。

两者同构（GradStatusData 档位列表）：
  TotalPayActivityData{records:[GradStatusData], activityTime, isStart}
  TotalConsumeActivityData{record.status:[GradStatusData], totalConsume, ...}
GradStatusData{Grade, GradeConsume, status, awards, payAwards}
  · Grade = 档位目标值（累计充值=dollar / 消费=consume）——客户端领奖
    请求 id=Grade、回包按 Grade 匹配刷新（TotalPayCell 实证）。
  · status：0 未达成 / 1 可领 / 2 已领。
  · payAwards：额外奖励列（仅消费活动的部分档有）。
"""

from ...proto import ps_protobuf_pb2 as pb
from . import common
from .one_pay import _fill_spec


def _st(name):
    st = common.act_state().setdefault(name, {"claimed": []})
    # 7 天轮次重置（累计充值：本轮充值额清零；累计消耗：dollar_used 另有 sync 处理）
    cfg = common.activity_cfg(name) or {}
    rd = int(cfg.get("round_days") or 7)
    if common.round_reset(st, rd):
        st["claimed"] = []
        if name == "total_pay":
            st["round_pay"] = 0
    return st


def _cur_value(name):
    if name == "total_pay":
        st = _st("total_pay")
        return int(st.get("round_pay") or 0)
    sync_dollar_consume()
    st = common.act_state().setdefault("total_consume", {})
    return int(st.get("dollar_used") or 0)


def sync_dollar_consume():
    """累计消耗活动（元宝）：**余额差值法**（2026-10-23 用户方案）。

    记录上次元宝余额快照 last_diamond；当前余额 < 快照 → 差值计入
    dollar_used（不管哪个渠道花的，只要余额减少就算，永不遗漏）。
    充值等余额上升不计入。**每轮开始清零**（round_days 一轮，默认 7 天）。
    """
    import time as _t
    cfg = common.activity_cfg("total_consume") or {}
    round_days = max(1, int(cfg.get("round_days") or 7))
    round_key = int(_t.time() // 86400) // round_days
    st = common.act_state().setdefault("total_consume", {})
    if st.get("round_key") != round_key:
        # 新一轮：清零计数 + 重置余额快照为当前值
        st["round_key"] = round_key
        st["dollar_used"] = 0
        st["last_diamond"] = None
        st.pop("claimed", None)
    cur = int(core_pay_diamond())
    last = st.get("last_diamond")
    if last is None:
        st["last_diamond"] = cur
        return
    if cur < int(last):
        st["dollar_used"] = int(st.get("dollar_used") or 0) + (int(last) - cur)
    st["last_diamond"] = cur


def core_pay_diamond():
    from .. import core
    return core.PLAYER_DATA.get("player", {}).get("diamond") or 0


def core_pay_dollar():
    from .. import core
    return core.PLAYER_DATA.get("player", {}).get("pay_dollar") or 0


def _snapshot_grades(m_list, name, grade_key, cur_value, claimed):
    """把配置档位灌进 GradStatusData 列表。"""
    cfg = common.activity_cfg(name) or {"grades": []}
    for g in cfg["grades"]:
        target = int(g[grade_key])
        d = m_list.add()
        d.Grade = target
        d.GradeConsume = target
        d.status = common.grade_status(cur_value, target, target in claimed)
        for a in g.get("awards") or []:
            _fill_spec(d.awards.add(), a)
        for a in g.get("payAwards") or []:
            _fill_spec(d.payAwards.add(), a)


def on_recharge(price):
    """充值事件：累计充值活动按**本轮**累计（7 天一轮清零）。"""
    st = _st("total_pay")
    st["round_pay"] = int(st.get("round_pay") or 0) + int(price or 0)
    return int(st["round_pay"])


def total_pay_snapshot():
    cfg = common.activity_cfg("total_pay")
    if not cfg:
        return None
    start, end, is_open = common.time_window(cfg)
    if not is_open:
        return None
    st = _st("total_pay")
    m = pb.TotalPayActivityData()
    m.activityTime.startTime = int(start)
    m.activityTime.endTime = int(end)
    m.isStart = 1
    m.totalPay = _cur_value("total_pay")
    _snapshot_grades(m.record.status, "total_pay", "dollar", _cur_value("total_pay"),
                     set(st.get("claimed") or []))
    return m


def total_consume_snapshot():
    cfg = common.activity_cfg("total_consume")
    if not cfg:
        return None
    start, end, is_open = common.time_window(cfg)
    if not is_open:
        return None
    sync_dollar_consume()
    st = _st("total_consume")
    m = pb.TotalConsumeActivityData()
    m.activityTime.startTime = int(start)
    m.activityTime.endTime = int(end)
    m.isStart = 1
    m.totalConsume = _cur_value("total_consume")
    _snapshot_grades(m.record.status, "total_consume", "consume",
                     _cur_value("total_consume"), set(st.get("claimed") or []))
    return m


def claim(name, grade_key, grade):
    """通用档位领奖。grade=档位目标值（Grade 回显值）。"""
    cfg = common.activity_cfg(name)
    if not cfg:
        return False, "活动未开启", []
    target = int(grade)
    st = _st(name)
    claimed = set(st.get("claimed") or [])
    hit = None
    for g in cfg["grades"]:
        if int(g[grade_key]) == target:
            hit = g
            break
    if hit is None:
        return False, "档位不存在", []
    if target in claimed:
        return False, "该档已领取", []
    if _cur_value(name) < target:
        return False, "未达到该档", []
    claimed.add(target)
    st["claimed"] = sorted(claimed)
    return True, "领取成功", hit.get("awards") or []
