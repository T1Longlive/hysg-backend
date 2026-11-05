"""在线奖励活动（2026-10-23 M4，客户端权威链路）。

不走 MissionConfig（研究结论：在线奖励在原版是独立通道）：
  EnterGame(25).activityData.onlineAwardRecord
      = OnlineAwardRecordData{grade, minutes, targetMinutes, awards}
  · grade 1-based：当前待领档（0 = 全部领完）
  · minutes/targetMinutes：客户端以此为基准**本地计时**判定达标+红点
  · 领奖：GetOnlineAward(10005)（空 body）→ 服务端发奖+推进档 →
    回 10005 + OnlineAwardRecordData（新档）
  · 档位配置 = 服务端运营数据（activities.json 的 online_award.grades）
  · 跨天重置：grade 回第 1 档、minutes 归零（每日重新领）
"""

import time

from ...proto import ps_protobuf_pb2 as pb
from . import common
from .one_pay import _fill_spec


def _st():
    return common.act_state().setdefault(
        "online_award", {"grade": 1, "minutes_base": 0.0, "sync_ts": 0, "day": ""})


def _today():
    return time.strftime("%Y%m%d")


def _refresh_day():
    """跨天重置（每日从第一档重新领）。"""
    st = _st()
    if st.get("day") != _today():
        st["day"] = _today()
        st["grade"] = 1
        st["minutes_base"] = 0.0
        # ★ 2026-10-28 修：跨天重置时 sync_ts 必须置为**当前时刻**而不是 0。
        #   0 会让 cur_minutes() 的增量部分恒为 0（"从 0 时刻起算"）→
        #   跨天后一直在线的玩家时长卡在 0、领奖被拒"在线时长不足"，
        #   客户端本地计时却照常走 → 红点亮着点不动（"没到账/没刷新"）。
        st["sync_ts"] = int(time.time())
    return st


def cur_minutes():
    """当前累计在线分钟（含自上次 sync 至今的增量）。"""
    st = _refresh_day()
    base = float(st.get("minutes_base") or 0)
    sync = int(st.get("sync_ts") or 0)
    now = int(time.time())
    add = (now - sync) / 60.0 if sync > 0 else 0.0
    return base + add


def _touch_sync():
    """把当前累计写回基准（登录/领奖时调用，客户端随后以此续算）。"""
    st = _refresh_day()
    st["minutes_base"] = round(cur_minutes(), 2)
    st["sync_ts"] = int(time.time())
    return st


def snapshot():
    """OnlineAwardRecordData；未配置/跨天自动处理。"""
    cfg = common.activity_cfg("online_award")
    st = _refresh_day()
    grades = (cfg or {}).get("grades") or []
    grade = int(st.get("grade") or 0)
    m = pb.OnlineAwardRecordData()
    if cfg and 1 <= grade <= len(grades):
        g = grades[grade - 1]
        m.grade = grade
        m.minutes = round(cur_minutes())
        m.targetMinutes = int(g["minute"])
        for a in g.get("awards") or []:
            _fill_spec(m.awards.add(), a)
    else:
        # 全部领完/无配置：grade=0 + targetMinutes=0（客户端判定无奖可领）
        m.grade = 0
        m.minutes = round(cur_minutes())
        m.targetMinutes = 0
    return m


def claim():
    """领取当前档（10005）。返回 (ok, why, awards)。"""
    cfg = common.activity_cfg("online_award")
    if not cfg:
        return False, "活动未开启", []
    st = _refresh_day()
    grades = cfg.get("grades") or []
    grade = int(st.get("grade") or 0)
    if not (1 <= grade <= len(grades)):
        return False, "在线奖励已领完", []
    target = int(grades[grade - 1]["minute"])
    if cur_minutes() < target:
        return False, "在线时长不足（%d/%d 分钟）" % (int(cur_minutes()), target), []
    awards = grades[grade - 1].get("awards") or []
    st["grade"] = grade + 1
    _touch_sync()
    return True, "领取成功", awards
