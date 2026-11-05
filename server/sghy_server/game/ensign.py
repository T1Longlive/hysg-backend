"""军旗系统业务模块（2026-10-23）。

数据模型（PLAYER_DATA 顶层键，经 repo 的 _top 机制自动持久化）：
  data["flags"]        {str(flag_id): {id, index}}                       ← FlagData
  data["flag_parts"]   {str(part_id): {id, type, host, unlock_count,
                                       up_count, additions[]}}          ← FlagPartData
  data["warbook"]      {map_id, lv, lv_exp, start_draw_state, current_event,
                       divination_count, war_map_tips[], add_bag, add_per,
                       reduce_cost, bagua{}}                            ← WarBookRecordData（跑商）
  data["squad_flags"]  {str(category): {str(index): flag_id}}           ← 编队上阵军旗

规则来源（用户 2026-10-23 口述 + 客户端实证，详见 work/军旗系统_后端开发设计.md）：
  · 部件词条按 FlagPartConfig.Addition 池全量生成、全部解锁；
  · 制作初始 value = MaxValue 随机 10%~30%，baseValue 存初值；
  · 升级一次所有词条 value += floor(MaxValue×0.2)，封顶 MaxValue（5 次升满）；
  · 分解返还 = (ProductCost + UpCost×up_count + UnlockCost×unlock_count) × 0.8，
    只退材料（ShowType 201-206）和铜币，图纸不退（对齐客户端 mergeItems）；
  · 材料折算：ItemConfig.Functions Replacement{ID=低一级, ReplaceCount=4}
    = 1 高级 = 4 低级，扣料时服务端自动向下折算；
  · 旗帐上限 GameSetConfig.MaxFlagCount=24，解锁花 UnlockFlagCost=1000 钻；
  · 上阵 = 写 squad_flags，编队/LevelBegin.myGroup.flagParts 消费。
"""

import math
import random
import re

from . import game_config

# FlagPartData.host 的「未装备」值（客户端判 >0 才挂载）
HOST_NONE = 0

# 材料类 ShowType（分解返还范围；图纸 207/208 不退）
MATERIAL_SHOW_TYPES = {201, 202, 203, 204, 205, 206}


def _data():
    """延迟取 PLAYER_DATA（避免与 core 的模块级循环导入）。"""
    from . import core
    return core.PLAYER_DATA


# ---------------------------------------------------------------- store

def flag_store(data=None):
    data = data or _data()
    store = data.setdefault("flags", {})
    if not store:
        # 首个旗帐免费赠送（客户端 EnsignBearerCell 只对 id==0 的空位显示解锁按钮）
        store["1"] = {"id": 1, "index": 1}
    return store


def part_store(data=None):
    return (data or _data()).setdefault("flag_parts", {})


def warbook_store(data=None):
    data = data or _data()
    wb = data.setdefault("warbook", {})
    wb.setdefault("map_id", 0)
    wb.setdefault("lv", 0)
    wb.setdefault("lv_exp", 0)
    wb.setdefault("start_draw_state", 0)
    wb.setdefault("current_event", 0)
    wb.setdefault("divination_count", 0)
    wb.setdefault("war_map_tips", [])
    wb.setdefault("add_bag", 0)
    wb.setdefault("add_per", 0)
    wb.setdefault("reduce_cost", 0)
    wb.setdefault("bagua", {})
    return wb


def squad_flag_store(data=None):
    return (data or _data()).setdefault("squad_flags", {})


def get_flag(flag_id, data=None):
    return flag_store(data).get(str(int(flag_id or 0)))


def get_part(part_id, data=None):
    return part_store(data).get(str(int(part_id or 0)))


def _next_part_id(data=None):
    store = part_store(data)
    return (max((int(k) for k in store), default=50000)) + 1


def _next_flag_id(data=None):
    store = flag_store(data)
    return (max((int(k) for k in store), default=0)) + 1


# ---------------------------------------------------------------- 材料/消耗

_REP_CACHE = {}


def item_replacement(itype):
    """ItemConfig.Functions 里的 Replacement{ID=低一级, ReplaceCount=N}。

    注意不能用 load_item_config 的 func 字段（那是 Functions[1]，材料行的
    Functions[1] 常是 Synthetic）——这里按行内正则精确找 Replacement 段。
    返回 (low_id, ratio) 或 None。
    """
    itype = int(itype)
    if itype in _REP_CACHE:
        return _REP_CACHE[itype]
    out = None
    path = game_config._CONFIG_DIR + "/app.config.new.ItemConfig.lua"
    src = game_config._read_cached(path)
    m = re.search(r"\[\s*%d\s*\]\s*=\s*\{" % itype, src)
    if m:
        seg = src[m.end():m.end() + 2500]
        rm = re.search(r'ClassName\s*=\s*"Replacement"\s*,\s*ID\s*=\s*(\d+)\s*,\s*'
                       r"ReplaceCount\s*=\s*(\d+)", seg)
        if rm:
            out = (int(rm.group(1)), int(rm.group(2)))
    _REP_CACHE[itype] = out
    return out


def item_count(itype, data=None):
    return int(((data or _data()).get("items") or {}).get(str(int(itype)), 0))


# 军旗材料箱（自选箱，可指定开布料/皮革/丝线/骨头）——按品质分 5 档
BOX_BY_QUALITY = {1: 21035, 2: 21036, 3: 21037, 4: 21038, 5: 21039}


def material_quality(itype):
    """军旗材料 id → 品质 1~5。

    21000-21019 每 5 个一组（布料/皮革/丝线/骨头，各自 Q1..Q5），
    所以 `(id - 21000) % 5 + 1` 就是品质。
    """
    it = int(itype)
    if 21000 <= it < 21020:
        return (it - 21000) % 5 + 1
    return 0


def consume_item(itype, need, data=None, allow_box=False):
    """扣道具（含 Replacement 4:1 向下折算）。

    allow_box=True：折算链断掉（低品质材料也没有）时，用**同品质材料箱**补足。
    这是客户端「便捷制作」的语义 —— 它把材料箱也算进配平
    （`EnsignManger` 的 getBagItems(itemBoxs[i]) 参与 totalNeedWhite 折算），
    服务端只认材料本身就会判「材料不足」，而界面显示「材料充足」
    （2026-10-26 用户报障：八卦旗冠 21009×40 不足，但前端显示够）。

    返回实际扣减 {type: n}（含原类型、折算消耗的低级材料、以及消耗的材料箱）；不够返回 None。
    """
    data = data or _data()
    items = data.setdefault("items", {})
    itype = int(itype)
    need = int(need)
    if need <= 0:
        return {}
    used = {}
    have = int(items.get(str(itype), 0))
    take = min(have, need)
    if take > 0:
        items[str(itype)] = have - take
        used[itype] = used.get(itype, 0) + take
        need -= take
    if need > 0:
        rep = item_replacement(itype)
        # 白级材料的 Replacement 指向自己（21000→21000 自环），必须跳过
        if rep and int(rep[0]) != itype:
            low_id, ratio = rep
            # ★ 递归层**不允许**用箱子：否则缺料会在最底层（Q1）一次性烧掉几千个
            #   低品质箱子（实测 9249 个 Q1 箱 + 435 个 Q2 箱换 43 个 Q5 材料），
            #   而客户端是"从高品质箱开始用"。箱子只在最外层按目标品质补。
            sub = consume_item(low_id, need * ratio, data, allow_box=False)
            if sub is not None:
                for k, v in sub.items():
                    used[k] = used.get(k, 0) + v
                need = 0
        # ★ 折算链断掉 → 用同品质材料箱补（便捷制作自动开箱）
        if need > 0 and allow_box:
            q = material_quality(itype)
            box = BOX_BY_QUALITY.get(q)
            if box:
                bh = int(items.get(str(box), 0))
                bt = min(bh, need)
                if bt > 0:
                    items[str(box)] = bh - bt
                    used[box] = used.get(box, 0) + bt
                    need -= bt
        if need > 0:
            # 真不够 → 全量回滚（含递归里已扣的部分由上游负责）
            for k, v in used.items():
                items[str(k)] = int(items.get(str(k), 0)) + v
            return None
    return used


def _white_value(item_id, box_quality=None):
    """材料/材料箱的「白级等价」价值：Q1=1, Q2=4, Q3=16, Q4=64, Q5=256。

    只对军旗材料（21000-21019）与材料箱（21035-21039）有效，其它返回 0
    （图纸等不可折算，走 1:1 同 id 校验）。
    """
    it = int(item_id)
    if 21000 <= it < 21020:
        return 4 ** (material_quality(it) - 1)
    inv = {v: k for k, v in BOX_BY_QUALITY.items()}
    if it in inv:
        return 4 ** (inv[it] - 1)
    return 0


def consume_plan(plan, awards, data=None):
    """按**客户端提交的扣料清单**扣料（= 界面预先显示的消耗内容）。

    plan: [(道具id, 数量)] —— 来自 MultIDCounteListByIDMessage.idCount[].idCount[]，
          客户端 `ConvenientForgingPanel` 把材料（各品质，高→低）和材料箱
          （各品质，高→低）都算好放在这里，服务端再自主折算就会和界面不一致。
    awards: 配置里的消耗（ProductCost / UpCost），只用于**校验**，防止客户端少扣。

    校验两条：
      1) 每种道具的数量都够（不够则整单拒绝，不扣）
      2) 材料白级等价总量 ≥ 配置需求（防作弊少扣）；图纸按同 id 1:1 校验
    返回 (used{type:n}, reason)；失败 used=None。
    """
    data = data or _data()
    items = data.setdefault("items", {})
    plan = [(int(i), int(c)) for i, c in (plan or []) if int(i) > 0 and int(c) > 0]
    if not plan:
        return None, "扣料清单为空"
    for iid, cnt in plan:
        if int(items.get(str(iid), 0)) < cnt:
            return None, "材料不足（%s ×%d）" % (iid, cnt)

    # 等价校验
    need_white = 0
    need_direct = {}
    for a in (awards or []):
        if str(a.get("ClassName") or "") == "PropertyAward":
            continue
        t = int(a.get("Type") or 0)
        c = int(a.get("Count") or 0)
        if t <= 0 or c <= 0:
            continue
        w = _white_value(t)
        if w > 0:
            need_white += c * w
        else:
            need_direct[t] = need_direct.get(t, 0) + c
    got_white = sum(_white_value(i) * c for i, c in plan)
    if got_white < need_white:
        return None, "材料不足（折算后 %d/%d）" % (got_white, need_white)
    plan_ids = {i: c for i, c in plan}
    for t, c in need_direct.items():
        if plan_ids.get(t, 0) < c:
            return None, "材料不足（%s ×%d）" % (t, c)

    used = {}
    for iid, cnt in plan:
        items[str(iid)] = int(items.get(str(iid), 0)) - cnt
        used[iid] = used.get(iid, 0) + cnt
    return used, "ok"


def consume_awards(awards, data=None, allow_box=False):
    """按 Award 数组扣消耗（ObjectAward 材料 + PropertyAward 铜币）。

    allow_box=True 时材料不足会用同品质材料箱补（见 consume_item）。
    返回 (ok, reason, item_deltas{type: -n}, coin_delta)。
    """
    data = data or _data()
    total_items = {}
    coin_cost = 0
    for a in (awards or []):
        cls = str(a.get("ClassName") or "")
        if cls == "PropertyAward":
            if str(a.get("PropertyName")) == "Coin":
                coin_cost += int(a.get("Value") or 0)
        else:  # ObjectAward 及未知类一律按道具处理
            itype = int(a.get("Type") or 0)
            cnt = int(a.get("Count") or 0)
            if itype <= 0 or cnt <= 0:
                continue
            used = consume_item(itype, cnt, data, allow_box=allow_box)
            if used is None:
                # 回滚已扣的
                _rollback(items_dict=data["items"], deltas=total_items)
                return False, "材料不足（%s ×%d）" % (itype, cnt), {}, 0
            for k, v in used.items():
                total_items[k] = total_items.get(k, 0) + v
    if coin_cost > 0:
        coin = int(data.get("player", {}).get("coin") or 0)
        if coin < coin_cost:
            _rollback(items_dict=data["items"], deltas=total_items)
            return False, "铜币不足（需 %d，有 %d）" % (coin_cost, coin), {}, 0
        data["player"]["coin"] = coin - coin_cost
    return True, "ok", total_items, coin_cost


def _rollback(items_dict, deltas):
    for k, v in (deltas or {}).items():
        items_dict[str(k)] = int(items_dict.get(str(k), 0)) + v


def grant_items(deltas, data=None):
    """发道具（分解返还用）：{type: n} 直接加进 items。"""
    items = (data or _data()).setdefault("items", {})
    for k, v in (deltas or {}).items():
        items[str(int(k))] = int(items.get(str(int(k)), 0)) + int(v)


# ---------------------------------------------------------------- 部件

def _cfg_awards(cfg, key):
    out = []
    for a in (cfg.get(key) or []):
        if not isinstance(a, dict):
            continue
        out.append(a)
    return out


def build_additions(cfg, rng=None):
    """制作时生成词条：配置 Addition 池全量、全部解锁、value=MaxValue 随机 10%~30%。"""
    rng = rng or random
    out = []
    for a in (cfg.get("Addition") or []):
        if not isinstance(a, dict):
            continue
        maxv = int(a.get("MaxValue") or 0)
        lo = int(math.ceil(maxv * 0.10))
        hi = max(lo, int(math.floor(maxv * 0.30)))
        base = rng.randint(lo, hi) if maxv > 0 else 0
        out.append({
            "type": int(a.get("Type") or 0),
            "value": base,
            "mode": int(a.get("Mode") or 0),
            "index": int(a.get("IndexID") or 0),
            "maxValue": maxv,
            "unlock": True,
            "baseValue": base,
        })
    return out


def make_part(part_type, data=None, rng=None, plan=None):
    """制作部件（5346）：扣 ProductCost → 新 FlagPartData 记录。
    返回 (ok, reason, part, item_deltas, coin_delta)

    plan: 客户端提交的扣料清单；给了就**照单扣**（与界面显示一致），
          没给才走服务端自主折算（allow_box 补材料箱）。
    """
    data = data or _data()
    cfg = game_config.flag_part_cfg(part_type)
    if not cfg:
        return False, "军旗部件配置不存在(%s)" % part_type, None, {}, 0
    awards = _cfg_awards(cfg, "ProductCost")
    if plan:
        used, why = consume_plan(plan, awards, data)
        if used is None:
            return False, why, None, {}, 0
        coin = 0
        for a in awards:
            if str(a.get("ClassName") or "") == "PropertyAward" \
                    and str(a.get("PropertyName")) == "Coin":
                coin += int(a.get("Value") or 0)
        if coin > 0:
            have = int(data.get("player", {}).get("coin") or 0)
            if have < coin:
                for k, v in used.items():
                    data["items"][str(k)] = int(data["items"].get(str(k), 0)) + v
                return False, "铜币不足（需 %d，有 %d）" % (coin, have), None, {}, 0
            data["player"]["coin"] = have - coin
        ok, reason, deltas = True, "ok", used
    else:
        ok, reason, deltas, coin = consume_awards(awards, data, allow_box=True)
    if not ok:
        return False, reason, None, deltas, coin
    pid = _next_part_id(data)
    additions = build_additions(cfg, rng)
    part = {
        "id": pid,
        "type": int(part_type),
        "host": HOST_NONE,
        "unlock_count": len(additions),   # 全部解锁（用户拍板）
        "up_count": 0,
        "additions": additions,
    }
    part_store(data)[str(pid)] = part
    return True, "ok", part, deltas, coin


UP_STEP_RATIO = 0.2    # 每次升级 +MaxValue×20%（5 次升满，用户拍板）


def level_up_part(part_id, data=None, plan=None):
    """升级部件（5347）：扣 UpCost → 所有词条 value += floor(MaxValue×0.2) 封顶。
    返回 (ok, reason, part, item_deltas, coin_delta)

    plan: 客户端扣料清单（便捷升级同格式）；给了照单扣，与界面显示一致。
    """
    data = data or _data()
    part = get_part(part_id, data)
    if not part:
        return False, "部件不存在(%s)" % part_id, None, {}, 0
    cfg = game_config.flag_part_cfg(part.get("type"))
    if not cfg:
        return False, "部件配置不存在(%s)" % part.get("type"), part, {}, 0
    if not any(int(a.get("value") or 0) < int(a.get("maxValue") or 0)
               for a in part.get("additions") or []):
        return False, "部件已满级", part, {}, 0
    awards = _cfg_awards(cfg, "UpCost")
    if plan:
        used, why = consume_plan(plan, awards, data)
        if used is None:
            return False, why, part, {}, 0
        coin = 0
        for a in awards:
            if str(a.get("ClassName") or "") == "PropertyAward" \
                    and str(a.get("PropertyName")) == "Coin":
                coin += int(a.get("Value") or 0)
        if coin > 0:
            have = int(data.get("player", {}).get("coin") or 0)
            if have < coin:
                for k, v in used.items():
                    data["items"][str(k)] = int(data["items"].get(str(k), 0)) + v
                return False, "铜币不足（需 %d，有 %d）" % (coin, have), part, {}, 0
            data["player"]["coin"] = have - coin
        ok, reason, deltas = True, "ok", used
    else:
        ok, reason, deltas, coin = consume_awards(awards, data, allow_box=True)
    if not ok:
        return False, reason, part, deltas, coin
    for a in part.get("additions") or []:
        maxv = int(a.get("maxValue") or 0)
        cur = int(a.get("value") or 0)
        if cur < maxv:
            step = max(1, int(math.floor(maxv * UP_STEP_RATIO)))
            a["value"] = min(maxv, cur + step)
    part["up_count"] = int(part.get("up_count") or 0) + 1
    return True, "ok", part, deltas, coin


def unlock_addition(part_id, data=None):
    """解锁词条（5348）：当前规则制作即全解锁 → 正常不会被触发；
    保留实现以防后续改初始解锁数。返回 (ok, reason, part, deltas, coin)"""
    data = data or _data()
    part = get_part(part_id, data)
    if not part:
        return False, "部件不存在(%s)" % part_id, None, {}, 0
    locked = [a for a in part.get("additions") or [] if not a.get("unlock")]
    if not locked:
        return False, "没有可解锁的词条", part, {}, 0
    cfg = game_config.flag_part_cfg(part.get("type"))
    if not cfg:
        return False, "部件配置不存在", part, {}, 0
    ok, reason, deltas, coin = consume_awards(_cfg_awards(cfg, "UnlockCost"), data, allow_box=True)
    if not ok:
        return False, reason, part, deltas, coin
    locked[0]["unlock"] = True
    part["unlock_count"] = int(part.get("unlock_count") or 0) + 1
    return True, "ok", part, deltas, coin


def resolve_refund(part, data=None):
    """分解返还：材料(ShowType 201-206)+铜币 ×0.8；图纸不退（对齐客户端 mergeItems）。
    返回 (item_deltas{type: n}, coin)"""
    data = data or _data()
    cfg = game_config.flag_part_cfg(part.get("type")) or {}
    merged_items = {}
    merged_coin = 0

    def merge(awards, times):
        nonlocal merged_coin
        for a in awards:
            if not isinstance(a, dict):
                continue
            cls = str(a.get("ClassName") or "")
            if cls == "PropertyAward":
                merged_coin += int(a.get("Value") or 0) * times
                continue
            itype = int(a.get("Type") or 0)
            icfg = game_config.get_item(itype) or {}
            if int(icfg.get("show_type") or 0) not in MATERIAL_SHOW_TYPES:
                continue    # 图纸等不返还
            merged_items[itype] = merged_items.get(itype, 0) + int(a.get("Count") or 0) * times

    merge(_cfg_awards(cfg, "ProductCost"), 1)
    merge(_cfg_awards(cfg, "UpCost"), int(part.get("up_count") or 0))
    merge(_cfg_awards(cfg, "UnlockCost"), int(part.get("unlock_count") or 0))
    item_out = {t: int(math.floor(c * 0.8)) for t, c in merged_items.items()}
    coin_out = int(math.floor(merged_coin * 0.8))
    return {t: n for t, n in item_out.items() if n > 0}, coin_out


def resolve_part(part_id, data=None):
    """分解部件（5349）：返还+删除。返回 (ok, reason, part_id, item_deltas, coin)"""
    data = data or _data()
    part = get_part(part_id, data)
    if not part:
        return False, "部件不存在(%s)" % part_id, None, {}, 0
    item_out, coin_out = resolve_refund(part, data)
    grant_items(item_out, data)
    if coin_out > 0:
        p = data.setdefault("player", {})
        p["coin"] = int(p.get("coin") or 0) + coin_out
    host = int(part.get("host") or 0)
    part_store(data).pop(str(int(part_id)), None)
    return True, "ok", int(part_id), item_out, coin_out


# ---------------------------------------------------------------- 旗帐

def unlock_flag(data=None):
    """解锁新旗帐（5356）：MaxFlagCount 上限，扣 UnlockFlagCost 钻。
    返回 (ok, reason, flag)"""
    data = data or _data()
    store = flag_store(data)
    max_cnt = game_config.ensign_game_set("MaxFlagCount", 24)
    if len(store) >= max_cnt:
        return False, "旗帐已达上限（%d）" % max_cnt, None
    cost = game_config.ensign_game_set("UnlockFlagCost", 1000)
    from . import core
    if not core.spend_diamond(cost, "UnlockFlag"):
        p = data.get("player", {})
        return False, "元宝不足（需 %d，有 %d）" % (cost, p.get("diamond")), None
    fid = _next_flag_id(data)
    index = max((int(v.get("index") or 0) for v in store.values()), default=0) + 1
    flag = {"id": fid, "index": index}
    store[str(fid)] = flag
    return True, "ok", flag


def parts_of_flag(flag_id, data=None):
    """某旗帐上的全部部件记录。"""
    out = []
    for p in part_store(data).values():
        if int(p.get("host") or 0) == int(flag_id):
            out.append(p)
    return out


def equip_part(part_id, flag_id, data=None):
    """装备部件到旗帐（5351）：同 host 同槽位唯一。返回 (ok, reason, part)"""
    data = data or _data()
    part = get_part(part_id, data)
    if not part:
        return False, "部件不存在(%s)" % part_id, None
    if not get_flag(flag_id, data):
        return False, "旗帐不存在(%s)" % flag_id, part
    cfg = game_config.flag_part_cfg(part.get("type")) or {}
    slot = int(cfg.get("Part") or 0)
    for other in parts_of_flag(flag_id, data):
        if int(other.get("id")) == int(part_id):
            continue
        oc = game_config.flag_part_cfg(other.get("type")) or {}
        if int(oc.get("Part") or 0) == slot:
            return False, "该槽位已装备部件", part
    part["host"] = int(flag_id)
    return True, "ok", part


def unequip_part(part_id, data=None):
    """卸下部件（5352）。返回 (ok, reason, part)"""
    data = data or _data()
    part = get_part(part_id, data)
    if not part:
        return False, "部件不存在(%s)" % part_id, None
    part["host"] = HOST_NONE
    return True, "ok", part


# ---------------------------------------------------------------- 上阵

def get_squad_flag(category, index, data=None):
    store = squad_flag_store(data)
    return int((store.get(str(int(category))) or {}).get(str(int(index)), 0))


def set_squad_flag(category, index, flag_id, data=None):
    store = squad_flag_store(data)
    bucket = store.setdefault(str(int(category)), {})
    if int(flag_id or 0) > 0:
        bucket[str(int(index))] = int(flag_id)
    else:
        bucket.pop(str(int(index)), None)


def set_squad_flag_checked(category, index, flag_id, data=None):
    """上阵校验：旗帐存在且至少有 1 个部件。返回 (ok, reason)"""
    data = data or _data()
    if int(flag_id or 0) <= 0:
        set_squad_flag(category, index, 0, data)
        return True, "ok"
    if not get_flag(flag_id, data):
        return False, "旗帐不存在(%s)" % flag_id
    if not parts_of_flag(flag_id, data):
        return False, "军旗没有部件，不能上阵"
    set_squad_flag(category, index, flag_id, data)
    return True, "ok"


def squad_flag_part_types(category, index, data=None):
    """编队军旗的部件 type 列表（LevelBegin.myGroup.flagParts / 战斗数据用）。"""
    flag_id = get_squad_flag(category, index, data)
    if flag_id <= 0:
        return []
    return sorted(int(p.get("type") or 0) for p in parts_of_flag(flag_id, data))


# 套装属性规则 —— 用户 2026-10-28 定稿（客户端 tooltip 核对）：
#   全游戏只有这四套给「攻击/防御/生命」加成，其余套装（东吴1103、涅槃1304、
#   贪婪1200、烈火燎原1300…）**没有任何属性加成**（它们只有战斗被动技能）。
#     · 战鼓 1100：无势力限制，2 件 → 攻/防/生 各 +3%（配置 Additions 驱动）
#     · 蜀汉 1101：仅 Origin=2（蜀）上阵武将，2 件 +5% / 4 件 +10%
#     · 曹魏 1102：仅 Origin=1（魏）上阵武将，2 件 +5% / 4 件 +10%
#     · 群雄 1104：仅 Origin=4（群）上阵武将，2 件 +7% / 4 件 +12%
#   数值不在配置 AddPowerPer（配置里四套都是 {2,3}，与实际不符），按 tooltip 硬编码。
#   Group 与势力的对应以**部件配置名**为准：44521「曹魏旗冠」=1102、
#   46521「群雄旗冠」=1104、44501「蜀汉旗冠」=1101。
_SUIT_ATTR_RULE = {
    1100: (None, (), True),        # 战鼓：走配置 Additions（2 件 3%×3 属性）
    1101: (2, (5, 10), False),     # 蜀汉
    1102: (1, (5, 10), False),     # 曹魏
    1104: (4, (7, 12), False),     # 群雄
}


def squad_flag_suit_additions(category, index, data=None, hero_origin=None):
    """上阵旗帐的**套装属性加成** [(AdditionType, value, mode)]。

    只认 `_SUIT_ATTR_RULE` 里的四套（战鼓/蜀汉/曹魏/群雄），按部件 Group 计数，
    取已达成的最高档、不累积。阵营套按 hero_origin（HeroConfig.Origin）过滤：
    只对该阵营的上阵武将生效；hero_origin=None（不区分武将）时不过滤。
    其余套装（含东吴1103、涅槃1304）一律不产出属性加成。
    产出直接喂 growth.apply_addition_bonus(..., EQUIP_TYPE_ATTR)。
    """
    flag_id = get_squad_flag(category, index, data)
    if flag_id <= 0:
        return []
    groups = {}
    for p in parts_of_flag(flag_id, data):
        cfg = game_config.flag_part_cfg(p.get("type")) or {}
        g = int(cfg.get("Group") or 0)
        if g > 0:
            groups[g] = groups.get(g, 0) + 1
    if not groups:
        return []
    suits = game_config.load_flag_suit_config() or {}
    out = []
    for gid, cnt in groups.items():
        rule = _SUIT_ATTR_RULE.get(int(gid))
        if not rule:
            continue
        origin, tiers, use_cfg = rule
        if origin is not None and hero_origin is not None \
                and int(origin) != int(hero_origin):
            continue
        if use_cfg:
            s = suits.get(int(gid)) or {}
            counts = [game_config._toi(x) for x in (s.get("Count") or [])]
            adds = s.get("Additions") or []
            best = -1
            for i, need in enumerate(counts):
                if cnt >= need and i < len(adds):
                    best = i
            if best < 0:
                continue
            for a in (adds[best] or []):
                if not isinstance(a, dict):
                    continue
                out.append((int(a.get("Type") or 0), int(a.get("Value") or 0),
                            int(a.get("Mode") or 0)))
            continue
        s = suits.get(int(gid)) or {}
        counts = [game_config._toi(x) for x in (s.get("Count") or [])]
        val = 0
        for i, need in enumerate(counts):
            if cnt >= need and i < len(tiers):
                val = tiers[i]          # 取已达成的最高档
        if val > 0:
            out.extend([(9, val, 1), (1, val, 1), (2, val, 1)])  # 生命/攻击/防御%
    return out


def squad_flag_bonuses(category, index, data=None, hero_origin=None):
    """编队上阵军旗 → 武将的属性加成 [(AdditionType, value, mode)]。

    两部分相加：
      1. 装备在旗帐上的部件「已解锁词条」（value>0，Mode 0 固定 / 1 百分比）
         —— 对编队全体上阵武将生效（无势力限定）
      2. **套装加成**（见 squad_flag_suit_additions）：只有战鼓/蜀汉/曹魏/群雄
         四套给 生命/攻击/防御%，阵营套只给该阵营上阵武将；其余套装无属性加成
    顺序有意义：词条（固定值）在前、套装（百分比）在后，
    `apply_addition_bonus` 按列表顺序累加，百分比会作用在前面已加过的值上。
    产出直接喂 growth.apply_addition_bonus(..., EQUIP_TYPE_ATTR)。
    """
    flag_id = get_squad_flag(category, index, data)
    if flag_id <= 0:
        return []
    out = []
    for p in parts_of_flag(flag_id, data):
        for a in (p.get("additions") or []):
            if not a.get("unlock"):
                continue
            val = int(a.get("value") or 0)
            if val == 0:
                continue
            out.append((int(a.get("type") or 0), val, int(a.get("mode") or 0)))
    out.extend(squad_flag_suit_additions(category, index, data,
                                         hero_origin=hero_origin))
    return out


def squad_flag_bonuses_for_hero(hero_type, category=0, index=1, data=None):
    """按武将取编队军旗加成：部件词条全体生效，套装按势力过滤。

    hero 的势力取 HeroConfig.Origin（growth.hero_origin）；查不到（None）时
    套装不做势力过滤（无势力限定套装照常生效，势力套装按旧口径也生效——
    宁多勿错，避免配置缺 Origin 把加成整段吃掉）。
    """
    try:
        from . import growth
        origin = growth.hero_origin(hero_type)
    except Exception:
        origin = None
    return squad_flag_bonuses(category, index, data=data, hero_origin=origin)


# ---------------------------------------------------------------- 跑商（商行/马车）
# 状态机（2026-10-23 M5，语义钉死自 EnsignBusinessPanel 实读）：
#   31204 选图（写 map_id，重置轮次）→ 31207 开跑（耗 1 张门票，start_draw_state=1）
#   → 循环 [31203 前进=遇商人（扣铜币、按 DrawStageInfo 档位权重掷 AwardID 池、
#     奖品进临时背包 bagua.remain、current_event=1）→ 31208 领取事件（清
#     current_event、divination_count+1）] → 临时背包满 → 31206 结算（货物入真背包、
#     加 FlagDrawConfig.Exp 马车经验）→ 回选图；或 31205 放弃（货物保留一半）。
# 每步 100% 遇商人（全部地图 EventRandom={Type=1,Probability=100}）。
# bagua.isSuccess = 保底（运气判定，FlagDrawLevelConfig.Probability 每级 +5%）：
#   保底时强制取概率>0 的最高档（自定规则，原版无据）。

# ---------------------------------------------------------------- 图纸合成（5355）
# 入口协议反查（2026-10-23）：EnsignBlueprintMakePanel 确认合成 →
#   socket:send("IDCounteListByIDMessage", 5355 UseFlagPartBlueprintGetRewards,
#               {id=FlagPartSpecialSyntheticConfig.ID, idCount=[{id=图纸type, count}]})
#   ack = 5355 **AwardResult**（面板自解播合成动画，不走 301 弹窗）。
# 规则（FlagPartSpecialSyntheticConfig）：Cost=[{Condition=图纸品质, Value=张数}]，
# 只能放品质==Condition 的图纸、总数==Value；Awards=[QuoteAward→AwardsConfig 池]。

def blueprint_synthetic(make_id, id_counts, data=None, rng=None):
    """图纸合成（5355）。返回 (ok, reason, granted[{type,count}], consumed)。"""
    data = data or _data()
    rng = rng or random
    cfgs = game_config.load_flag_synthetic_config()
    if isinstance(cfgs, list):
        cfgs = {i + 1: row for i, row in enumerate(cfgs)}
    cfg = (cfgs or {}).get(int(make_id or 0))
    if not cfg:
        return False, "合成目标不存在(%s)" % make_id, [], []
    costs = [c for c in (cfg.get("Cost") or []) if isinstance(c, dict)]
    if not costs:
        return False, "合成目标缺少 Cost 配置", [], []
    need_quality = int(costs[0].get("Condition") or 0)
    need_count = int(costs[0].get("Value") or 0)

    # 校验：图纸品质匹配 + 总张数==Value
    total = 0
    plan = []
    for entry in (id_counts or []):
        itype = int((entry or {}).get("id") or 0)
        cnt = int((entry or {}).get("count") or 0)
        if itype <= 0 or cnt <= 0:
            continue
        icfg = game_config.get_item(itype)
        if not icfg:
            return False, "图纸不存在(%s)" % itype, [], []
        if int(icfg.get("quality") or 0) != need_quality:
            return False, "图纸品质不符（需 %d 品质）" % need_quality, [], []
        have = item_count(itype, data)
        if have < cnt:
            return False, "图纸数量不足（%s 需 %d 有 %d）" % (itype, cnt, have), [], []
        total += cnt
        plan.append((itype, cnt))
    if total != need_count:
        return False, "图纸数量不符（需 %d 张，选了 %d 张）" % (need_count, total), [], []

    # 扣图纸（按客户端选的类型精确扣，不走 4:1 折算）
    for itype, cnt in plan:
        items = data.setdefault("items", {})
        items[str(itype)] = int(items.get(str(itype), 0)) - cnt
    consumed = [{"type": t, "count": c} for t, c in plan]

    # 掷 Awards 池
    from . import lottery_config
    granted = []
    for a in (cfg.get("Awards") or []):
        if isinstance(a, dict) and int(a.get("AwardID") or 0) > 0:
            for r in lottery_config.roll_pool(int(a["AwardID"]), rng):
                if r.get("kind") != "item" or int(r.get("type") or 0) <= 0:
                    continue
                granted.append({"type": int(r["type"]), "count": max(1, int(r.get("count") or 1))})
    if granted:
        grant_items({g["type"]: g["count"] for g in granted}, data)
    return True, "ok", granted, consumed


def warbook_map_cfg(map_id):
    cfg = game_config.load_flag_draw_config()
    if isinstance(cfg, list):
        cfg = {i + 1: row for i, row in enumerate(cfg)}
    return (cfg or {}).get(int(map_id or 0))


def warbook_capacity(data=None):
    """临时背包容量 = FlagDrawLevelConfig[马车lv].TempBagCapacity + addBagFlagEffect。"""
    data = data or _data()
    wb = warbook_store(data)
    lv = int(wb.get("lv") or 0)
    cfgs = game_config.load_flag_draw_level_config()
    if isinstance(cfgs, list):
        cfgs = {i + 1: row for i, row in enumerate(cfgs)}
    cap = 3
    for row in cfgs.values():
        if int(row.get("Lv") or 0) == lv:
            cap = int(row.get("TempBagCapacity") or 3)
            break
    return cap + int(wb.get("add_bag") or 0)


def warbook_luck(data=None):
    """运气加成（%）：FlagDrawLevelConfig[lv].Probability。"""
    data = data or _data()
    wb = warbook_store(data)
    lv = int(wb.get("lv") or 0)
    cfgs = game_config.load_flag_draw_level_config()
    if isinstance(cfgs, list):
        cfgs = {i + 1: row for i, row in enumerate(cfgs)}
    for row in cfgs.values():
        if int(row.get("Lv") or 0) == lv:
            return int(row.get("Probability") or 0)
    return 0


def warbook_choose_map(map_id, data=None):
    """31204 选图：校验存在且开放；写 map_id 并重置轮次状态。"""
    data = data or _data()
    cfg = warbook_map_cfg(map_id)
    if not cfg:
        return False, "跑商地图不存在(%s)" % map_id
    if int(cfg.get("Show") or 0) != 1:
        return False, "该路线尚未开放"
    wb = warbook_store(data)
    wb["map_id"] = int(map_id)
    wb["start_draw_state"] = 0
    wb["current_event"] = 0
    wb["bagua"] = {}
    return True, "ok"


def warbook_start(data=None, rng=None):
    """31207 开跑：校验马车等级/门票 → 耗 1 张门票 → start_draw_state=1。"""
    data = data or _data()
    rng = rng or random
    wb = warbook_store(data)
    map_id = int(wb.get("map_id") or 0)
    cfg = warbook_map_cfg(map_id)
    if not cfg:
        return False, "请先选择跑商路线"
    if int(wb.get("lv") or 0) < int(cfg.get("NeedLv") or 0):
        return False, "马车等级不足（需 %d 级）" % int(cfg.get("NeedLv") or 0)
    if int(wb.get("start_draw_state") or 0) == 1:
        return False, "本轮跑商进行中"
    consume = cfg.get("Consume")
    if isinstance(consume, (int, float)) and int(consume) > 0:
        used = consume_item(int(consume), 1, data)
        if used is None:
            return False, "跑商门票不足（%s）" % int(consume)
    wb["start_draw_state"] = 1
    wb["current_event"] = 0
    wb["bagua"] = {}
    return True, "ok"


def warbook_divination(data=None, rng=None):
    """31203 前进（100% 遇商人）：扣铜币 → 掷档位 → 奖品进临时背包。

    返回 (ok, reason, event(1=商人), coin_cost, gained[{type,value}])。
    bagua 状态直接写进 warbook（调用方取快照组包）。
    """
    data = data or _data()
    rng = rng or random
    wb = warbook_store(data)
    map_id = int(wb.get("map_id") or 0)
    cfg = warbook_map_cfg(map_id)
    if not cfg:
        return False, "请先选择跑商路线", 0, 0, []
    if int(wb.get("start_draw_state") or 0) != 1:
        return False, "本轮跑商未开始", 0, 0, []
    cost = max(0, int(cfg.get("DivinationNeedCoin") or 0)
               - int(wb.get("reduce_cost") or 0))
    p = data.setdefault("player", {})
    coin = int(p.get("coin") or 0)
    if coin < cost:
        return False, "铜币不足（需 %d，有 %d）" % (cost, coin), 0, 0, []
    p["coin"] = coin - cost

    stages = [s for s in (cfg.get("DrawStageInfo") or []) if isinstance(s, dict)]
    if not stages:
        return False, "地图缺少 DrawStageInfo 配置", 1, cost, []
    luck = warbook_luck(data)
    is_success = rng.random() * 100 < luck
    if is_success:
        # 保底：取概率>0 的最高档
        cands = [s for s in stages if int(s.get("Probability") or 0) > 0]
        stage = max(cands or stages, key=lambda s: int(s.get("BaguaStatus") or 0))
    else:
        weights = [(s, max(0, int(s.get("Probability") or 0))) for s in stages]
        total = sum(w for _, w in weights)
        if total <= 0:
            stage = stages[-1]
        else:
            pick = rng.random() * total
            acc = 0
            stage = weights[-1][0]
            for s, w in weights:
                acc += w
                if pick < acc:
                    stage = s
                    break

    bag = wb.setdefault("bagua", {})
    bag["current_result"] = int(stage.get("BaguaStatus") or 1)
    bag["is_success"] = bool(is_success)
    remain = bag.setdefault("remain", [])
    cap = warbook_capacity(data)
    gained = []
    if int(stage.get("AwardID") or 0) > 0:
        from . import lottery_config
        for a in lottery_config.roll_pool(int(stage["AwardID"]), rng):
            if a.get("kind") != "item" or int(a.get("type") or 0) <= 0:
                continue
            if len(remain) >= cap:
                break        # 容量满（客户端在满时会先走结算，不会掷到这）
            entry = {"type": int(a["type"]), "value": max(1, int(a.get("count") or 1))}
            remain.append(entry)
            gained.append(entry)
    wb["current_event"] = 1
    return True, "ok", 1, cost, gained


def warbook_event_done(data=None):
    """31208 领取商人事件：清 current_event、divination_count+1。"""
    data = data or _data()
    wb = warbook_store(data)
    if int(wb.get("current_event") or 0) != 1:
        return False, "当前没有可领取的商人事件"
    wb["current_event"] = 0
    wb["divination_count"] = int(wb.get("divination_count") or 0) + 1
    return True, "ok"


def warbook_round_reset(wb):
    wb["start_draw_state"] = 0
    wb["current_event"] = 0
    wb["bagua"] = {}


def warbook_settle(data=None):
    """31206 结算：临时背包入真背包 + FlagDrawConfig.Exp 马车经验 + 重置轮次。

    返回 (ok, reason, granted[{kind,type,count}], lv)
    """
    data = data or _data()
    wb = warbook_store(data)
    if int(wb.get("start_draw_state") or 0) != 1:
        return False, "没有进行中的跑商", [], int(wb.get("lv") or 0)
    bag = wb.get("bagua") or {}
    remain = bag.get("remain") or []
    granted = []
    for entry in remain:
        t, v = int(entry.get("type") or 0), int(entry.get("value") or 0)
        if t <= 0 or v <= 0:
            continue
        grant_items({t: v}, data)
        granted.append({"kind": "item", "type": t, "count": v})
    map_id = int(wb.get("map_id") or 0)
    cfg = warbook_map_cfg(map_id) or {}
    lv = add_warbook_exp(int(cfg.get("Exp") or 0), data)
    warbook_round_reset(wb)
    return True, "ok", granted, lv


def warbook_giveup(data=None):
    """31205 放弃：货物保留一半（ceil(value/2)，损失一半为客户端文案语义）+ 重置轮次。"""
    data = data or _data()
    wb = warbook_store(data)
    if int(wb.get("start_draw_state") or 0) != 1:
        return False, "没有进行中的跑商", []
    bag = wb.get("bagua") or {}
    remain = bag.get("remain") or []
    kept = []
    for entry in remain:
        t, v = int(entry.get("type") or 0), int(entry.get("value") or 0)
        if t <= 0 or v <= 0:
            continue
        keep = v - v // 2          # ceil(v/2)：保留一半（向上取整）
        if keep <= 0:
            continue
        grant_items({t: keep}, data)
        kept.append({"kind": "item", "type": t, "count": keep})
    warbook_round_reset(wb)
    return True, "ok", kept


def warbook_remove_tips(remove_ids, data=None):
    """31209 移除地图红点提示，返回剩余 tips 列表。"""
    data = data or _data()
    wb = warbook_store(data)
    tips = [int(x) for x in (wb.get("war_map_tips") or [])]
    remove = {int(x) for x in (remove_ids or [])}
    tips = [x for x in tips if x not in remove]
    wb["war_map_tips"] = tips
    return tips


def warbook_snapshot(data=None):
    """bagua 快照（组包用）：{current_result, is_success, remain[{type,value}]}"""
    wb = warbook_store(data)
    bag = wb.get("bagua") or {}
    return {
        "current_result": int(bag.get("current_result") or 0),
        "is_success": bool(bag.get("is_success")),
        "remain": [dict(x) for x in (bag.get("remain") or [])],
    }


def add_warbook_exp(exp, data=None):
    """跑商结算加马车经验，按 FlagDrawLevelConfig 升级。

    ★ 语义以客户端为准（refreshLvTxt：经验条显示 lvExp/当前行 NeedExp）——
    **当前等级行的 NeedExp = 升到 lv+1 所需**（lv0 行 NeedExp=1、lv1 行 5、
    lv2 行 20、lv3 行 60）。此前误用"下一级行的 NeedExp"导致玩家看着经验
    满了却不升级（2026-10-23 实机报障）。
    返回升级后的 lv。"""
    data = data or _data()
    wb = warbook_store(data)
    wb["lv_exp"] = int(wb.get("lv_exp") or 0) + int(exp or 0)
    cfgs = game_config.load_flag_draw_level_config()
    if isinstance(cfgs, list):                     # 连续 [1..n] 会被折叠成 list
        cfgs = {i + 1: row for i, row in enumerate(cfgs)}
    by_lv = {int(row.get("Lv") or 0): row for row in cfgs.values()}
    changed = True
    while changed:
        changed = False
        cur_lv = int(wb.get("lv") or 0)
        row = by_lv.get(cur_lv)
        if row and wb["lv_exp"] >= int(row.get("NeedExp") or 0):
            wb["lv_exp"] -= int(row.get("NeedExp") or 0)
            wb["lv"] = cur_lv + 1
            changed = True
    return int(wb["lv"])
