"""周期刷新任务：页签1 每日/每周/每月（Cycle 1/2/3）+ 页签2 三餐体力（Cycle 6）。

口径（2026-10-29 用户确认）：
  · Cycle=1 每日：万人斩·击败100/200敌人（10045/10047）、挑战BOSS关卡10次（10048）
      —— 每日计数器，0 点重置
  · Cycle=2 每周：获得武将50次（10054）—— 每周计数器，周一 0 点重置
  · Cycle=3 每月：合成武将2次（10056）—— 每月计数器，1 号 0 点重置
  · Cycle=6 限时：三餐体力（10303/10304/10305）
      TriggerValue=开窗(HHMM)、Period=闭窗(HHMM)：早 7:00-9:00 / 午 12:00-14:00 /
      晚 19:00-21:00，窗口内可领 Ep 60；窗口外不下发

排除（明确不做）：
  · 10049 竞技场（target=2）—— 用户明确不要
  · 10592~17503 登录送扫荡卷（target=-1）—— daily_token 模块已实现，避免双份
  · 18201 消耗体力（target=47）、18101/18102 跑商（46）、19201~19504 战令周任务
    （52 消耗铜钱/3 获得装备/25 挑战副本/47）—— 待后续

事件挂钩：mission_achieve.bump_counter 会同步把 kill/boss 计入每日、hero_get 计入
每周、compose 计入每月（见 on_event）。跨天/跨周/跨月时：本会话发过的周期任务
先 503 删除再 501 重发（`push_refresh`），登录时 EnterGame 直接带新周期实例。

存档：`PLAYER_DATA["mission_periodic"] = {"day","week","month","claimed":{id:周期键}}`
"""

import datetime
import time

KEY = "mission_periodic"
PERIODIC_TARGETS = {1: (0, 1), 2: (9,), 3: (11,), 6: (-1,)}


def _data():
    from . import core
    return core.PLAYER_DATA


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 配置

_ROWS = None


def _rows():
    global _ROWS
    if _ROWS is not None:
        return _ROWS
    from . import game_config
    cfg = game_config.load_mission_config() or {}
    out = []
    for mid, row in cfg.items():
        c = _int(row.get("cycle"))
        if c not in PERIODIC_TARGETS:
            continue
        t = _int(row.get("target"))
        if t not in PERIODIC_TARGETS[c]:
            continue
        out.append(row)
    out.sort(key=lambda r: _int(r["id"]))
    _ROWS = out
    return _ROWS


def _is_meal(row):
    return _int(row.get("cycle")) == 6


# ---------------------------------------------------------------- 时间/周期

def _now():
    return datetime.datetime.now()


def _now_hhmm():
    t = _now()
    return t.hour * 100 + t.minute


def _period_keys(now=None):
    t = now or _now()
    iso = t.isocalendar()
    return {"d": t.strftime("%Y-%m-%d"),
            "w": "%d-W%02d" % (iso[0], iso[1]),
            "m": t.strftime("%Y-%m")}


def _in_window(row, hhmm=None):
    """三餐时间窗：TriggerValue=开窗 HHMM，Period=闭窗 HHMM。"""
    start, end = _int(row.get("trigger_value")), _int(row.get("period"))
    if start <= 0 or end <= 0:
        return False
    v = hhmm if hhmm is not None else _now_hhmm()
    return start <= v <= end


# ---------------------------------------------------------------- 状态

def state(data=None):
    st = (data or _data()).setdefault(KEY, {})
    keys = _period_keys()
    if (st.get("day") != keys["d"] or st.get("week") != keys["w"]
            or st.get("month") != keys["m"]):
        # 跨周期：旧 claim 全部过期 —— 记下 id 供 push_refresh 推 503（客户端还挂着旧实例）
        st["expired"] = [_int(k) for k in (st.get("claimed") or {})]
        st["day"], st["week"], st["month"] = keys["d"], keys["w"], keys["m"]
        st["claimed"] = {}
        st.setdefault("counters", {}).clear()
    st.setdefault("expired", [])
    st.setdefault("claimed", {})
    st.setdefault("counters", {})
    return st


def counters(data=None):
    return state(data)["counters"]


def bump(key, n=1, data=None):
    """周期计数（每日 kill/boss、每周 hero、每月 compose）。"""
    c = counters(data)
    c[key] = _int(c.get(key)) + max(0, _int(n))
    return c[key]


def _claimed_now(row, data=None):
    st = state(data)
    pk = _claim_key(row)
    return st["claimed"].get(str(row["id"])) == pk


def _claim_key(row):
    c = _int(row.get("cycle"))
    keys = _period_keys()
    return {"d": keys["d"], "w": keys["w"], "m": keys["m"]}.get(
        {1: "d", 2: "w", 3: "m"}.get(c, "d")) if c != 6 else keys["d"]


def on_event(key, n=1, data=None):
    """mission_achieve.bump_counter 转发：终身计数 → 周期计数。"""
    m = {"kill": ("d", "kill"), "boss": ("d", "boss"),
         "hero_get": ("w", "hero"), "compose": ("m", "compose")}
    if key not in m:
        return
    pk, ck = m[key]
    st = state(data)
    if st.get("day") != _period_keys()["d"] and pk == "d":
        return          # 状态未滚动（理论上 state() 已处理）
    st["counters"][ck] = _int(st["counters"].get(ck)) + max(0, _int(n))


# ---------------------------------------------------------------- 任务

def progress(row, data=None):
    """返回 (complete, need)。"""
    t = _int(row.get("target"))
    need = max(1, _int(row.get("target_value")))
    c = _int(row.get("cycle"))
    if _is_meal(row):
        return 1, 1
    key = {0: "kill", 1: "boss", 9: "hero", 11: "compose"}.get(t)
    if key is None:
        return 0, need
    val = _int(counters(data).get(key))
    return min(need, max(0, val)), need


def visible(row, data=None, hhmm=None):
    if _claimed_now(row, data):
        return False
    if _is_meal(row):
        return _in_window(row, hhmm)
    return True


def snapshot_missions(data=None, hhmm=None):
    now = int(time.time())
    out = []
    for row in _rows():
        if not visible(row, data, hhmm):
            continue
        complete, need = progress(row, data)
        out.append({"id": _int(row["id"]), "type": _int(row["id"]),
                    "need": int(need), "complete": int(complete),
                    "accept": now})
    return out


def claim(mid, data=None):
    """领奖（10100 兜底）。返回 (ok, reason, row, awards)。"""
    mid = _int(mid)
    row = next((r for r in _rows() if _int(r["id"]) == mid), None)
    if not row:
        return False, "任务不存在", None, []
    if not visible(row, data):
        if _is_meal(row):
            return False, "不在领取时间段内", None, []
        return False, "任务不存在", None, []
    complete, need = progress(row, data)
    if complete < need:
        return False, "任务尚未完成", None, []
    from . import mission as _m
    awards = row.get("awards") or []
    # Ep 奖励走 economy（带恢复/上限口径），其余走通用发放
    if any(a.get("kind") == "property" and a.get("prop") == "Ep" for a in awards):
        from . import economy
        for a in awards:
            if a.get("kind") == "property" and a.get("prop") == "Ep":
                economy.add(_int(a.get("value")), data)
        rest = [a for a in awards
                if not (a.get("kind") == "property" and a.get("prop") == "Ep")]
        if rest:
            _m._grant(rest)
    else:
        _m._grant(awards)
    st = state(data)
    st["claimed"][str(mid)] = _claim_key(row)
    return True, "", row, awards


# ---------------------------------------------------------------- 推送

def push_refresh(session, data=None, force=False):
    """跨周期/窗口变化时：先 503 删过期实例，再 501/502 发新实例（按会话去重）。"""
    from . import core
    sent = getattr(session, "_periodic_sent", None)
    if sent is None:
        sent = {}
        session._periodic_sent = sent
    st = state(data)
    expired = st.get("expired") or []
    if expired or force:
        for mid in expired:
            session.send(core.OP_DELETE_MISSION, core.f_int(1, int(mid)))
            sent.pop(int(mid), None)
        st["expired"] = []
    for m in snapshot_missions(data):
        mid = _int(m["id"])
        comp = _int(m["complete"])
        if sent.get(mid) == comp:
            continue        # 进度没变不重推（否则每次战斗刷一屏 502）
        sent[mid] = comp
        if comp >= _int(m["need"]):
            session.send(core.OP_UPDATE_MISSION, core.build_mission_data(m))
        else:
            session.send(core.OP_NEW_MISSION, core.build_mission_data(m))


def dispatch_tick(session, data=None):
    """dispatch 开头的轻量巡检（每会话 30 秒一次）：三餐窗口开合 / 跨天刷新。"""
    now = time.time()
    last = getattr(session, "_periodic_tick", 0)
    if now - last < 30:
        return
    session._periodic_tick = now
    try:
        push_refresh(session, data)
    except Exception:
        pass
