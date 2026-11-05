"""连续登录活动（LoginDays，2026-10-23 M3）。

数据结构：LoginDaysActivityData{record: StatuesRecordsData{status:[GradStatusData]},
activityTime, isStart}——档位 GradStatusData{Grade=第N天, GradeConsume=N, status,
awards}。领奖 GetLoginDaysAward(5324, IDMessage{id=天数})。

进度规则（连续登录）：登录时推进——昨天也登录过 → days+1；断签 → 重置为 1。
每日首次进入（SelectCharacter）触发 on_login()。
"""

import datetime

from ...proto import ps_protobuf_pb2 as pb
from . import common
from .one_pay import _fill_spec


def _st():
    return common.act_state().setdefault("login_days", {"days": 0, "last_day": ""})


def on_login():
    """登录推进（SelectCharacter 时调用）。返回当前连续天数。"""
    today = datetime.date.today()
    ymd = "%04d%02d%02d" % (today.year, today.month, today.day)
    st = _st()
    if st.get("last_day") == ymd:
        return int(st.get("days") or 0)
    last = st.get("last_day") or ""
    if last:
        try:
            ld = datetime.date(int(last[:4]), int(last[4:6]), int(last[6:]))
            delta = (today - ld).days
            st["days"] = int(st.get("days") or 0) + 1 if delta == 1 else 1
        except ValueError:
            st["days"] = 1
    else:
        st["days"] = 1
    st["last_day"] = ymd
    return int(st["days"])


def snapshot():
    cfg = common.activity_cfg("login_days")
    if not cfg:
        return None
    start, end, is_open = common.time_window(cfg)
    if not is_open:
        return None
    st = _st()
    days = int(st.get("days") or 0)
    claimed = set(st.get("claimed") or [])
    m = pb.LoginDaysActivityData()
    m.activityTime.startTime = int(start)
    m.activityTime.endTime = int(end)
    m.isStart = 1
    for g in cfg["grades"]:
        n = int(g["day"])
        d = m.record.status.add()
        d.Grade = n
        d.GradeConsume = n
        d.status = common.grade_status(days, n, n in claimed)
        for a in g.get("awards") or []:
            _fill_spec(d.awards.add(), a)
    return m


def claim(day):
    """领奖（5324）。day=第几天。返回 (ok, why, awards)。"""
    cfg = common.activity_cfg("login_days")
    if not cfg:
        return False, "活动未开启", []
    n = int(day)
    st = _st()
    claimed = set(st.get("claimed") or [])
    hit = next((g for g in cfg["grades"] if int(g["day"]) == n), None)
    if hit is None:
        return False, "档位不存在", []
    if int(st.get("days") or 0) < n:
        return False, "登录天数不足", []
    if n in claimed:
        return False, "该档已领取", []
    claimed.add(n)
    st["claimed"] = sorted(claimed)
    return True, "领取成功", hit.get("awards") or []
