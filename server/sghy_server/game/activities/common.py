"""活动系统公共框架（2026-10-23，M1）。

提供三类基础设施（各活动模块复用）：
  1. 时间窗引擎 time_window()：配置 → ActivityTime{start,end} + is_open
  2. 档位状态机 grade_status()：进度 + 档位目标 + 已领 → status(0/1/2)
  3. 配置/存档访问：load_activity_config() / act_state()

配置形态（sghy_server/config/activities.json）：
  {"<activity>": {"enabled": true, "window": {...}, "grades": [...]},
   ...}
  window 支持：{"always": true} / {"start": unix, "end": unix}
             / {"openDays": N}（开服 N 天内，基准=玩家注册时间）
  window.enabled=false 或缺配置 → 活动关闭（客户端 isStart=-1，入口不显）。

档位 status 语义（客户端 ActivityManager:845 实证）：
  0=未达成 / 1=可领取 / 2=已领取
"""

import json
import os
import time

from .. import core

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ), "config", "activities.json")

# 档位状态（客户端语义）
GRADE_NOT_REACHED = 0
GRADE_CAN_CLAIM = 1
GRADE_CLAIMED = 2

_CONFIG_CACHE = None


def load_activity_config():
    """读运营配置（进程内缓存一次）。"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                _CONFIG_CACHE = json.load(f)
        except (OSError, ValueError):
            _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def activity_cfg(name):
    """取单个活动配置；未配置/未启用 → None（活动关闭）。"""
    cfg = load_activity_config().get(name)
    if not cfg or not cfg.get("enabled"):
        return None
    return cfg


def player_register_time():
    """玩家注册时间（unix 秒）——openDays 时间窗基准；缺省用存档里的
    register_time，再缺省视为当前时间（老存档兜底）。"""
    p = core.PLAYER_DATA.get("player", {})
    rt = p.get("register_time")
    if not rt:
        rt = int(time.time())
        p["register_time"] = rt
    return int(rt)


def time_window(cfg):
    """时间窗 → (start, end, is_open)。

    end=None 表示永久（转成很大的数，proto int64 可容纳）。
    always 窗口：start=注册时间、end=+100 年（客户端倒计时
    `endTime - now` 需要 end>now，发 0 会变巨额负数异常）。
    """
    now = int(time.time())
    w = (cfg or {}).get("window") or {}
    # ★ 配置了 round_days 的活动：窗口 = **本轮 7 天**（前端倒计时显示 ≤7 天，
    #   轮次一到自动进入下一轮 = 永久开启 + 每轮重置，2026-10-23 用户要求）
    if cfg and int(cfg.get("round_days") or 0) > 0:
        rd = int(cfg["round_days"])
        end = round_end_ts(rd)
        return end - rd * 86400, end, True
    if w.get("always"):
        start = player_register_time()
        return start, start + 100 * 365 * 86400, True
    if "openDays" in w:
        start = player_register_time()
        end = start + int(w["openDays"]) * 86400
    else:
        start = int(w.get("start") or 0)
        end = w.get("end")
        end = int(end) if end is not None else None
    is_open = now >= start and (end is None or now <= end)
    return start, end, is_open


def grade_status(cur_value, target_value, claimed):
    """档位三态：未达成/可领取/已领取。"""
    if claimed:
        return GRADE_CLAIMED
    if cur_value is not None and cur_value >= target_value:
        return GRADE_CAN_CLAIM
    return GRADE_NOT_REACHED


def act_state():
    """活动进度存档（PLAYER_DATA 顶层键，持久化自动兜底）。"""
    return core.PLAYER_DATA.setdefault("activity", {})


def is_midnight_ts(ts=None):
    """次日 0 点时间戳（每日刷新类活动用）。"""
    t = time.localtime(ts if ts is not None else time.time())
    nxt = time.struct_time((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0,
                            t.tm_wday, t.tm_yday, t.tm_isdst))
    return int(time.mktime(nxt))

def round_key(round_days=7, ts=None):
    """轮次标识（epoch 天数 // round_days）。轮次变化 = 新一轮开始 → 各活动重置。

    2026-10-23 用户方案：活动永久开启，但每 7 天重置刷新一次进度/次数。
    """
    day = int((ts if ts is not None else time.time()) // 86400)
    return day // max(1, int(round_days))


def round_reset(st, round_days=7):
    """通用轮次重置：返回 True 表示发生了轮次切换（调用方随后清自己的进度）。

    st 为该活动的存档 dict（会写入 round_key）。
    """
    rk = round_key(round_days)
    if st.get("round_key") != rk:
        st["round_key"] = rk
        return True
    return False


def round_end_ts(round_days=7, ts=None):
    """当前轮次的结束时间戳（下一个轮次开始点），商店刷新倒计时用。"""
    day = int((ts if ts is not None else time.time()) // 86400)
    rk = day // max(1, int(round_days))
    return (rk + 1) * int(round_days) * 86400

