"""主线任务系统（MissionConfig 里 MainIndex>0 的部分，2026-10-25）。

客户端事实（MissionManager / MissionData / MissionTabelCell 实读）：

  · 任务纯展示，**没有任何进度计算** —— 进度只能服务端攒，靠推送刷新：
      501 NewMission(S→C MissionData) / 502 UpdateMission(S→C MissionData)
      503 DeleteMission(S→C IDMessage{id}) / 504 MissionSuccess(S→C {id,type,AwardResult})
  · 领奖是 **10100 SubmitMission(C→S IDMessage{id=任务实例id})**（不是 504，504 是回包）
  · 完成判定：`needValue <= completeValue`（主线 needValue=1 → completeValue=1 即可领）
  · `onCompleteMissionHandler` 只把 type 记进 _completedMissionList，**不会自己删任务**
    → 服务端必须补推 503 才能把领过的任务从列表里拿掉

主线配置形态（game_config.load_mission_main，见其文档）：
  147 个节点，MainIndex 1..147，`Target` 全 7（通关指定关卡）、`TargetValue` 全 1、
  `Cycle` 全 0（一次性）、奖励为元宝 20~50。所以主线 = 一条"按顺序打通指定关卡"的链。

存档：`PLAYER_DATA["mission_main"] = {top, cleared, accept}`
  · top     = 已领奖的主线节点数（0 = 还没完成第一个；等于序列长度表示全部完成）
  · cleared = 当前节点（序列[top]）是否已通关、待领奖
  · accept  = 当前任务接取时间戳（下发给 acceptDate）
"""

import time

KEY = "mission_main"


def _data():
    from . import core
    return core.PLAYER_DATA


def _seq():
    from . import game_config
    return game_config.load_mission_main()


def state(data=None):
    d = data or _data()
    st = d.setdefault(KEY, {})
    st["top"] = int(st.get("top") or 0)
    st["cleared"] = bool(st.get("cleared"))
    if not st.get("accept"):
        st["accept"] = int(time.time())
    return st


def current(data=None):
    """当前应做的主线节点；全部完成返回 None。"""
    seq = _seq()
    st = state(data)
    idx = st["top"]
    if idx < 0 or idx >= len(seq):
        return None
    return seq[idx]


def snapshot(data=None):
    """EnterGame 用：(missions 列表, completed 配置 id 列表)。

    missions 每项：{id=实例id(=配置id), type, need, complete, accept}
    completed：已领奖节点的配置 id（客户端 completedMission，用于主线红点/战令判定）
    """
    seq = _seq()
    st = state(data)
    top = max(0, min(st["top"], len(seq)))
    done = [int(seq[i]["id"]) for i in range(top)]
    missions = []
    cur = current(data)
    if cur:
        missions.append({
            "id": int(cur["id"]),
            "type": int(cur["id"]),
            "need": int(cur.get("target_value") or 1),
            "complete": 1 if st["cleared"] else 0,
            "accept": int(st.get("accept") or 0),
        })
    return missions, done


def on_level_win(level_id, data=None):
    """战斗胜利结算：如果正好是当前主线节点要求的关卡 → 标记已通关。

    返回 (changed, node)：changed=True 时调用方推 502 UpdateMission。
    """
    st = state(data)
    cur = current(data)
    if not cur or st["cleared"]:
        return False, cur
    if int(cur.get("target_id") or 0) != int(level_id):
        return False, cur
    st["cleared"] = True
    return True, cur


def _grant(awards):
    """发放奖励（属性类 → 玩家字段；道具类 → items）。返回 [(名称/type, 数量)]。"""
    from . import core
    p = _data().setdefault("player", {})
    got = []
    prop_map = {"Dollar": "diamond", "Coin": "coin", "Ep": "ep", "Vigour": "vigour",
                "HeroCoin": "heroCoin"}
    for a in (awards or []):
        if a.get("kind") == "property":
            prop = str(a.get("prop") or "")
            val = int(a.get("value") or 0)
            key = prop_map.get(prop)
            if key and val:
                p[key] = int(p.get(key) or 0) + val
            got.append((prop, val))
        else:
            tid = int(a.get("type") or 0)
            cnt = int(a.get("count") or 0)
            if tid > 0 and cnt > 0:
                # 卡类（武将卡/定缘卡）由 add_item 按实例入库并广播 401
                core.add_item(tid, cnt)
            got.append((tid, cnt))
    return got


def sync_with_records(level_records=None, data=None):
    """按**已通关记录**把当前节点标成"已达成"（登录 / 领奖后调用）。

    为什么必须做：主线是按顺序推进的链，新接手的存档往往早就通关了前面的关卡
    （用户存档已打通 1002/1005/1010...），如果不追溯，玩家会看到"要你再打一次 1001"
    的任务，永远不会自己变成可领取。

    主线是链式的，一次只处理当前节点；领奖推进后调用方再调一次即可继续往下推。
    """
    from . import core
    recs = level_records if level_records is not None else core.LEVEL_RECORDS
    st = state(data)
    cur = current(data)
    if not cur or st["cleared"]:
        return False
    try:
        done = {int(k) for k in (recs or {})}
    except (TypeError, ValueError):
        done = set()
    if int(cur.get("target_id") or 0) in done:
        st["cleared"] = True
        return True
    return False


def claim(mission_id, data=None):
    """领奖（10100）。返回 (ok, reason, node, awards)。

    成功后 top += 1、cleared 归零；若下一个节点的关卡历史已通关，会立刻标成可领。
    调用方负责：
      · 推 504 MissionSuccess{id, type, AwardResult}
      · 推 503 DeleteMission{id}（客户端不会自己删）
      · 若还有下一个节点，推 501 NewMission
      · 推 200（元宝/铜钱变了）
    """
    st = state(data)
    cur = current(data)
    if not cur:
        return False, "主线任务已全部完成", None, []
    if int(mission_id) != int(cur["id"]):
        return False, "任务不存在或不是当前主线任务", None, []
    if not st["cleared"]:
        return False, "任务尚未完成", None, []
    awards = cur.get("awards") or []
    _grant(awards)
    st["top"] += 1
    st["cleared"] = False
    st["accept"] = int(time.time())
    sync_with_records(data=data)      # 下一个节点可能历史已通关 → 直接可领
    return True, "", cur, awards
