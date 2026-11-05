"""每日扫荡令牌（限时任务，2026-10-29）。

配置事实（MissionConfig 实读，27 条 cycle=1 且奖励含 20004 扫荡令牌）：

    id=10592  min_vip=0 max_vip=0   -> 令牌 ×10   （= VIPSetConfig[0].ReapLevelTimes）
    id=10593  min_vip=1 max_vip=1   -> ×15
    id=10594  min_vip=2 max_vip=2   -> ×20
    ...                             （与 VIPSetConfig[VIP].ReapLevelTimes 完全一致）

客户端事实（MissionManager / MissionData / MissionPanel 实读）：
  · 任务靠服务端推：501 NewMission(MissionData) / 502 UpdateMission / 503 DeleteMission /
    504 MissionSuccess{id,type,AwardResult}
  · 领取走 **10100 SubmitMission(IDMessage{id})**，不是 504
  · 完成判定 `needValue <= completeValue`；cycle=1 的任务面板按"每日"分区展示

实现口径：登录/进游戏时若当日未领，推一条匹配当前 VIP 的令牌任务（need=1/complete=1，
即"可领取"），玩家点领取 → 10100 → 发令牌 → 504 + 503 移除。跨天重置。
"""

import time

from . import game_config

KEY = "daily_token"
TOKEN_TYPE = 20004


def _data():
    from . import core
    return core.PLAYER_DATA


def _today():
    return time.strftime("%Y-%m-%d")


def _vip():
    return int((_data().get("player") or {}).get("vip") or 0)


def mission_row(vip=None):
    """当日应发的令牌任务（MissionConfig 里 cycle=1、奖励含 20004、VIP 区间匹配）。"""
    vip = _vip() if vip is None else int(vip)
    rows = game_config.load_mission_config() or {}
    best = None
    for m in rows.values():
        if int(m.get("cycle") or 0) != 1:
            continue
        if not any(int(a.get("type") or 0) == TOKEN_TYPE
                   for a in (m.get("awards") or []) if isinstance(a, dict)):
            continue
        lo = int(m.get("min_vip") or 0)
        hi = int(m.get("max_vip") or 0)
        if lo <= vip <= hi:
            # 同 VIP 档有多条时取 id 最大的（配置里从低到高递增）
            if best is None or int(m.get("id")) > int(best.get("id")):
                best = m
    return best


def state():
    st = _data().setdefault(KEY, {})
    today = _today()
    if st.get("day") != today:
        st["day"] = today
        st["claimed"] = False
        st["mission_id"] = 0
    return st


def pending():
    """当日可领的任务配置行；已领/无配置返回 None。"""
    st = state()
    if st.get("claimed"):
        return None
    row = mission_row()
    if not row:
        return None
    st["mission_id"] = int(row.get("id"))
    return row


def push_new(session):
    """登录后推 501（若当日还有可领的令牌任务）。返回是否推送。"""
    from . import core
    row = pending()
    if not row:
        return False
    st = state()
    accept = int(st.get("accept") or 0)
    if accept <= 0:
        accept = int(time.time())
        st["accept"] = accept
    session.send(core.OP_NEW_MISSION, core.build_mission_data({
        "id": int(row.get("id")),
        "type": int(row.get("id")),
        "need": 1,
        "complete": 1,
        "accept": accept,
    }))
    session.log("每日令牌任务 %s（VIP%d）→ 501 可领取" % (row.get("id"), _vip()))
    return True


def claim(mission_id):
    """领取：返回 (ok, reason, row, awards)。"""
    row = pending()
    if not row or int(row.get("id")) != int(mission_id):
        return False, "任务不存在或今日已领取", None, []
    awards = []
    for a in (row.get("awards") or []):
        if not isinstance(a, dict):
            continue
        if int(a.get("type") or 0) == TOKEN_TYPE:
            awards.append({"kind": "object", "type": TOKEN_TYPE,
                           "count": int(a.get("count") or 0)})
    if not awards:
        return False, "任务配置无令牌奖励", None, []
    st = state()
    st["claimed"] = True
    st["claim_time"] = int(time.time())
    return True, "", row, awards


def token_gain_now(vip=None):
    """当日可领数量（仅展示/日志用）。"""
    row = mission_row(vip)
    if not row:
        return 0
    for a in (row.get("awards") or []):
        if isinstance(a, dict) and int(a.get("type") or 0) == TOKEN_TYPE:
            return int(a.get("count") or 0)
    return 0
