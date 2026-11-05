"""体力（Ep）系统（2026-10-29）。

客户端事实（User.lua / LevelManager.lua / MainPanel.lua 实读）：

  · 体力是**服务端权威**：客户端只读 CharacterData.ep(18) / maxEp(29) /
    nextGetEpTime(30)，自己从不扣减。
  · 上限 = `ExpSetConfig[玩家等级].MaxEp`（1 级 30，60 级 154）。
  · 恢复速度 = `GameSetConfig[1].EpRecover`：客户端按 `epRecoverUnit()/5` **点/分钟**
    显示倒计时 → 一点 = 5/EpRecover 分钟。EpRecover=1 时 = 300 秒/点。
  · 进关前客户端已自检 `me:ep() < levelData:getEp()` → 弹买体力框；服务端仍要兜底扣。
  · 队伍升级补体力：`ExpSetConfig[新等级].LvUpEp`。

存档：`PLAYER_DATA["player"]["ep"]` + `["ep_time"]`（上次结算恢复的时间戳）。
采用**惰性恢复**：任何一次读体力/扣体力前先按时间补，不跑定时器。
"""

import time

from . import game_config

DEFAULT_MAX_EP = 100
DEFAULT_RECOVER_SECONDS = 300      # 一点体力的恢复秒数（见上面 EpRecover 换算）


def _player(data=None):
    from . import core
    d = data if data is not None else core.PLAYER_DATA
    return d.setdefault("player", {})


def recover_seconds():
    """一点体力恢复所需秒数。EpRecover=1 → 300s。"""
    try:
        unit = float(game_config.load_game_set_config().get("EpRecover") or 1)
    except (TypeError, ValueError):
        unit = 1.0
    if unit <= 0:
        return DEFAULT_RECOVER_SECONDS
    return int(max(1, round(300.0 / unit)))


def ep_max(data=None):
    p = _player(data)
    lv = int(p.get("lv") or 1)
    rows = game_config.load_exp_set_config() or {}
    row = rows.get(lv) or rows.get(1) or {}
    try:
        return int(row.get("MaxEp") or DEFAULT_MAX_EP)
    except (TypeError, ValueError):
        return DEFAULT_MAX_EP


def sync(data=None):
    """惰性恢复：按经过时间补体力，返回当前体力。"""
    p = _player(data)
    cap = ep_max(data)
    ep = int(p.get("ep") or 0)
    now = int(time.time())
    if ep >= cap:
        p["ep"] = cap
        p["ep_time"] = now
        return cap
    last = int(p.get("ep_time") or 0)
    if last <= 0:                      # 老存档没有时间戳 → 从现在开始计时
        p["ep_time"] = now
        p["ep"] = ep
        return ep
    step = recover_seconds()
    gained = (now - last) // step
    if gained > 0:
        ep = min(cap, ep + int(gained))
        p["ep"] = ep
        # 保留不足一点的余量，避免每次读都吞掉零头
        p["ep_time"] = last + int(gained) * step if ep < cap else now
    else:
        p["ep"] = ep
    return ep


def next_get_time(data=None):
    """下一点体力的到达时间戳（CharacterData.nextGetEpTime=30 用）。满体力返回 0。"""
    p = _player(data)
    ep = sync(data)
    cap = ep_max(data)
    if ep >= cap:
        return 0
    last = int(p.get("ep_time") or 0) or int(time.time())
    return last + recover_seconds()


def spend(n, data=None):
    """扣体力。返回 (ok, reason)。不足时**不扣**。"""
    n = int(n or 0)
    if n <= 0:
        return True, ""
    ep = sync(data)
    if ep < n:
        return False, "体力不足（需要 %d，当前 %d）" % (n, ep)
    p = _player(data)
    p["ep"] = ep - n
    if p["ep"] < ep_max(data):
        # 开始/继续计恢复计时；只在原本满体力时重置起点
        if not p.get("ep_time") or ep >= ep_max(data):
            p["ep_time"] = int(time.time())
    return True, ""


def add(n, data=None):
    """加法（升级补体力等），不超过上限。"""
    n = int(n or 0)
    if n <= 0:
        return sync(data)
    p = _player(data)
    ep = sync(data)
    p["ep"] = min(ep_max(data), ep + n)
    if p["ep"] >= ep_max(data):
        p["ep_time"] = int(time.time())
    return p["ep"]


def on_team_level_up(new_lv, data=None):
    """队伍升级：按 ExpSetConfig[新等级].LvUpEp 补体力。"""
    rows = game_config.load_exp_set_config() or {}
    row = rows.get(int(new_lv)) or {}
    try:
        gain = int(row.get("LvUpEp") or 0)
    except (TypeError, ValueError):
        gain = 0
    return add(gain, data) if gain else sync(data)
