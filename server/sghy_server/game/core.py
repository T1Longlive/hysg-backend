"""三国的后裔（youguTV）本地 mock 服务端

用途：客户端已确认无法连真实服（服务器拒收 8.6 版本登录协议），这里提供本地替代。

协议（已从客户端运行时反编译核实）：
  TCP 首包   客户端 -> 服务端：裸 Base64(JSON + "\n")，无长度头
  TCP 请求   客户端 -> 服务端：[4B 10008][4B len][4B opCode][4B protoIndex][protobuf]
                              len = 8 + len(protobuf)（含 opCode+protoIndex）
  TCP 响应   服务端 -> 客户端：[4B len][4B opCode][protobuf]
                              len = 4 + len(protobuf)（含 opCode）
  所有整数均为小端。

HTTP（客户端原来请求 gateway.hysg.yougu.tv，靠 override 脚本重定向到这里）：
  /api/getserverlist     服务器列表
  /api/getupdateconfig   热更新配置
  /api/getannounce       公告

protobuf 用纯手写编码（只用到 varint 与 length-delimited 两种 wire type），零第三方依赖。
"""
import base64
import json
import os
import random
import time
import traceback

# 配置表解析 + 玩家数据持久化
import sys
from . import game_config
from . import growth   # 武将成长 / 属性合成 / 战力（口径对齐 Hysg_Refactor）
from . import lottery_config  # 招募/寻宝奖池（LotterySetConfig + AwardsConfig）
from . import collect_config  # 图鉴/收集（CollectionAwardConfig + ShowInCollect）
from . import battle_validate
from . import buy              # 买体力/买铜钱（2026-10-24）
from . import mission          # 主线任务（2026-10-25）
from . import mail             # 邮件系统（2026-10-26）
from . import ensign          # 军旗系统（2026-10-23；ensign 不在模块级回引 core）
from . import economy         # 体力（2026-10-29；Ep 恢复/扣减/上限）
from . import level_run       # 扫荡/每日次数/重置/掉落（2026-10-29）
from . import daily_token     # 每日扫荡令牌任务（2026-10-29）
from . import challenge        # 挑战副本：圣兽岛/金银岛/阵营副本（2026-10-29）
from . import mission_achieve  # 成就/收集类任务（Cycle=0 非主线，2026-10-29）
from . import mission_periodic  # 周期任务：每日/每周/每月/三餐体力（2026-10-29）
from .proto_map import (parse_typed, req_equip_hero, req_function, req_id,
                       req_id_count,
                        req_id_list, req_id_value, req_level_begin,
                        req_level_end, req_prop_action, req_string_message,
                        req_use_lottery, req_collection_award)


# ---- 武将属性 / 战力统一入口（服务端口径，客户端只负责显示）----
# ---------------------------------------------------------------- 请求级缓存
# 一次 dispatch 内大量重复计算（典型：EnterGame 打包 512 个武将，每个都要
# on_field_groups / squad_flag_bonuses / hero_attrs）→ 实测 on_field_groups
# 占登录耗时的一半（O(武将数²)）。这里用「请求级」缓存：dispatch 开头与
# save_player_data 清空，跨请求不复用，避免脏读。
_REQ_CACHE = {}


# 本次请求内已推过 404 NewHero 的武将实例 id。
# 客户端 `addWarriorData` 遇到重复 id 返回 nil，`onNewWarrior` 接着取
# `warrior:getGroup()` 就 LUA ERROR（2026-10-29 圣兽岛结算实测）。
# `_grant_sign_awards` 建实例时会 broadcast 404，`_push_new_heroes` 再推一次
# 就是重复 —— 用这个集合在请求边界内去重。
_HERO_404_SEEN = set()


def req_cache_clear():
    _REQ_CACHE.clear()


def _req_cache_get(key):
    try:
        return _REQ_CACHE[key]
    except KeyError:
        return None


def _req_cache_put(key, val):
    _REQ_CACHE[key] = val
    return val


def hero_attrs(type_id, lv=1, grade=0, quality=None):
    """按成长曲线算武将属性 + 战力；配置缺失时回落 HeroConfig 基础值。

    请求级缓存：EnterGame/200 里同一武将可能被算多次；缓存存副本，
    返回也是副本（调用方会 mutate stats["power"]）。
    """
    _k = ("attrs", int(type_id or 0), int(lv or 1), int(grade or 0), quality)
    _hit = _req_cache_get(_k)
    if _hit is not None:
        return dict(_hit)
    a = growth.hero_attrs(type_id, lv or 1, grade or 0, quality)
    if a:
        _req_cache_put(_k, dict(a))
        return a
    if a:
        return a
    s = game_config.get_hero_stats(type_id, lv or 1)
    a = {
        "maxHP": s.get("maxHP", 295), "attack": s.get("attack", 99),
        "defence": s.get("defence", 51), "speed": s.get("speed", 12),
        "comboRate": 0, "blockRate": 0, "counterRate": 10,
        "critRate": 0, "critDamage": 150,
    }
    a["power"] = growth.calc_power(a)
    return a


def team_power(only_positioned=True, extra_bonuses=None, include_squad_flag=True):
    """我方总战力：主城玩家栏显示的是上阵武将战力之和（含缘分加成）。

    extra_bonuses：编队级加成（军旗词条）；不传时默认取编队 0/1 上阵军旗的加成
    （include_squad_flag=False 可关掉）。军旗上阵后战力要涨——玩家对"军旗有没有用"
    的唯一直观反馈就是这个数，战斗内与界面必须同口径。
    ★ 2026-10-27 改按武将取：套装带势力限定（蜀汉等）只对该势力武将生效，
      所以每个武将的加成列表单独算（部件词条全体一致，套装部分按 Origin 过滤）。
    """
    total = 0
    for h in PLAYER_DATA.get("heroes", {}).values():
        if only_positioned and h.get("position", -1) <= 0:
            continue
        st = hero_attrs(h.get("type"), h.get("lv", 1), h.get("grade", 0))
        if extra_bonuses is not None:
            bonuses = extra_bonuses                      # 调用方显式给定 → 原样用
        elif include_squad_flag:
            bonuses = ensign.squad_flag_bonuses_for_hero(h.get("type"))
        else:
            bonuses = None
        apply_fate_to_stats(st, h, bonuses)
        total += st.get("power", 0)
    return total

# ---- 玩家数据持久化（JSON 文件）----
# 存档路径可配置：默认沿用 env/ 下那份（用户真实进度），不自动新建
_PLAYER_DATA_PATH = os.environ.get(
    "SGHY_PLAYER_DATA",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))), "env", "player_data.json"))

_DEFAULT_PLAYER_DATA = {
    "player": {"lv": 60, "exp": 0, "coin": 5000000, "diamond": 1000, "ep": 100, "vigour": 100},
    "heroes": {},  # 由 _init_all_heroes() 填充
    "level_records": {
        "1001": {"star": 3, "leftTimes": 0, "buyTimes": 0},
        "1002": {"star": 3, "leftTimes": 0, "buyTimes": 0},
    },
    "items": {
        # 升级用的三个符（ItemConfig SubType=4）：训练符10 / 鬼才符100 / 天灵符1000
        "20005": 999, "20006": 999, "20007": 999,
        # 缘分卡（替身卡）测试用：曹操 / 曹仁 / 郭嘉 / 贾诩 各 4·5·6 星若干
        "25000": 200, "25001": 200, "25002": 200,   # 曹操替身 4/5/6★
        "25009": 200, "25010": 200, "25011": 200,   # 曹仁替身 4/5/6★
        "25069": 200, "25070": 200, "25071": 200,   # 郭嘉替身 4/5/6★
        "25021": 200, "25022": 200, "25023": 200,   # 贾诩替身 4/5/6★
    },
    # 装备实例：type = EquipConfig.ID，host = 穿戴者武将实例 id（0=未穿戴）
    # 33115 青龙偃月刀(武) 33415 白银盔(盔) 33315 兽面连环铠(甲) 33215 赤兔(宝物)
    # ★ 赤兔 Group=321 正好是曹操的装备缘分（FateID 1149「装备赤兔时」）
    "equips": {
        "1": {"id": 1, "type": 33115, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
        "2": {"id": 2, "type": 33415, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
        "3": {"id": 3, "type": 33315, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
        "4": {"id": 4, "type": 33215, "host": 5, "strengthen": 0, "grade": 0, "grade_exp": 0},
    }
}

def _init_all_heroes():
    """初始化 10 个经典武将 × 3 星级(4星/5星/6星) × 2 副本 = 60 个武将卡。
    星级对应 HeroConfig Quality：4星=Q4(10014), 5星=Q5(10015), 6星=Q6(10016)。
    spine 形象：Q4 共用基础形象，Q5/Q6 各换不同 AvatarType。
    按 APK 原版架构：服务端下发 HeroData（含完整属性数值），客户端直接用。
    """
    hero_config = game_config.load_hero_config()
    # 10 个经典武将的 Q4 基础 ID（4星形态）
    base_hero_ids = [
        10014,  # 曹操 4星
        10124,  # 关羽 4星
        10134,  # 张飞 4星
        10144,  # 赵云 4星
        10114,  # 刘备 4星
        10184,  # 诸葛亮 4星
        10324,  # 吕布 4星
        10334,  # 貂蝉 4星
        10254,  # 周瑜 4星
        10344,  # 华佗 4星
    ]
    heroes = {}
    uid = 0
    for base_id in base_hero_ids:
        # 每个武将 3 个星级变体：Q4(4星), Q5(5星), Q6(6星)
        for q in range(3):
            hid = base_id + q  # 10014/10015/10016
            if hid not in hero_config:
                continue
            star_info = game_config.get_hero_star_info(hid)
            avatar_info = game_config.get_hero_avatar(hid)
            star_level = star_info["quality"]  # Q4=4星, Q5=5星, Q6=6星
            stats = hero_attrs(hid, 10, star_level)
            name = game_config.get_hero_name(hid)
            # 每个星级 2 个副本
            for copy in range(2):
                uid += 1
                heroes[str(uid)] = {
                    "id": uid, "type": hid, "lv": 10,
                    "grade": star_level, "star": star_level,
                    "exp": 0, "awaken": 0,       # 突破次数（每次 +5 上限）
                    "grade_exp": 0,              # 升阶进度（一阶 100 点）
                    # 只有 6★ 那一行 HeroConfig.CanBreakLevel=1，其余行都是 0
                    "can_break": growth.hero_can_break(hid),
                    "maxHP": stats.get("maxHP", 295),
                    "attack": stats.get("attack", 99),
                    "defence": stats.get("defence", 51),
                    "speed": stats.get("speed", 12),
                    "comboRate": 0, "blockRate": 0, "counterRate": 10,
                    "critRate": 0, "critDamage": 150,
                    "position": uid if uid <= 5 else 0,  # 前 5 个上阵；0 = 未上阵
                    "name": name,
                    "avatar_type": star_info["avatar_type"],
                    "spine": avatar_info["spine"],
                    "avatar": avatar_info["avatar"],
                }
    # 测试夹具：把曹操 4★/5★ 的首个实例顶到"满级满阶"，方便直接验证升星链路
    # （正式流程应先走升级/升阶，这里只是省掉前置操作）
    if heroes.get("1"):
        heroes["1"]["lv"] = growth.star_max_lv(4)
        heroes["1"]["grade"] = growth.MAX_GRADE
    if heroes.get("3"):
        heroes["3"]["lv"] = growth.star_max_lv(5)
        heroes["3"]["grade"] = growth.MAX_GRADE

    # ---- 圣兽（升星材料）：玄武/青龙/朱雀/白虎 × 1~5 星 ----
    # 圣兽不练级：等级永远等于该星级的等级上限（1★15 … 5★35），CanBreakLevel=0
    top = max(int(k) for k in heroes) if heroes else 0
    for base in (14001, 14011, 14021, 14031):
        for star in range(1, 6):
            bid = base + star - 1
            if bid not in hero_config:
                continue
            lv = growth.beast_lv(star)
            st = hero_attrs(bid, lv, 0)
            try:
                b_avatar = game_config.get_hero_avatar(bid)
                b_star = game_config.get_hero_star_info(bid)
            except Exception:
                b_avatar = {"spine": "", "avatar": ""}
                b_star = {"avatar_type": 0}
            for _ in range(3 if star in (4, 5) else 1):
                top += 1
                heroes[str(top)] = {
                    "id": top, "type": bid, "lv": lv,
                    "grade": 0, "star": star,
                    "exp": 0, "awaken": 0, "can_break": False,
                    "beast": True,
                    "maxHP": st.get("maxHP", 0), "attack": st.get("attack", 0),
                    "defence": st.get("defence", 0), "speed": st.get("speed", 0),
                    "comboRate": 0, "blockRate": 0, "counterRate": 0,
                    "critRate": 0, "critDamage": 150,
                    "position": 0,
                    "name": game_config.get_hero_name(bid),
                    "avatar_type": b_star.get("avatar_type", 0),
                    "spine": b_avatar.get("spine", ""),
                    "avatar": b_avatar.get("avatar", ""),
                }
    return heroes

def load_player_data():
    if os.path.exists(_PLAYER_DATA_PATH):
        try:
            with open(_PLAYER_DATA_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return json.loads(json.dumps(_DEFAULT_PLAYER_DATA))

def save_player_data(data):
    """写盘 + 失效请求级缓存（数据已变，后续读取必须重算）。

    ★ 运行时本函数会被 __main__ 替换为 store.mark_dirty（DB 落库，带防抖）。
    """
    req_cache_clear()
    with open(_PLAYER_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def flush_now():
    """立即落库（关键操作后调用）。

    默认与 save_player_data 等价；接上 DB store 后由 __main__ 替换为
    store.flush_soon（把 0.5s 防抖窗口压到 0）。用于：充值、发邮件、装备养成。
    """
    return save_player_data(PLAYER_DATA)

# 是否把曹操 4★/5★ 的第一个实例置为"满级满阶"，方便直接验证升星（mock 专用）
_SEED_STARUP_READY = True

# 启动时加载
PLAYER_DATA = load_player_data()
if not PLAYER_DATA.get("heroes"):
    PLAYER_DATA["heroes"] = _init_all_heroes()
    save_player_data(PLAYER_DATA)
    print("[init] 已初始化 %d 个武将到 player_data.json" % len(PLAYER_DATA["heroes"]))
else:
    # 老存档迁移：补圣兽 + position 的 -1 统一成 0（客户端材料列表按 position==0 筛）
    _heroes = PLAYER_DATA["heroes"]
    _changed = False
    # 养成（升星 10~20 万 / 突破 20~160 万）需要充足金币，mock 账号兜底给足
    if PLAYER_DATA.get("player", {}).get("coin", 0) < 5000000:
        PLAYER_DATA.setdefault("player", {})["coin"] = 5000000
        _changed = True
    for _h in _heroes.values():
        if _h.get("position", 0) < 0:
            _h["position"] = 0
            _changed = True
        # 老存档缺字段兜底（养成相关字段是后加的）
        for _k, _dv in (("exp", 0), ("grade_exp", 0), ("awaken", 0)):
            if _k not in _h:
                _h[_k] = _dv
                _changed = True
        # ★ 2026-10-28：can_break 一律按**配置**重算（以前只补缺失、不纠正错值）。
        #   客户端 canBreakLv() 读 HeroConfig.CanBreakLevel，服务端却信存档里的
        #   过期标志 → 无双吕布(id=97) 存档 can_break=0，突破后上限卡 40 不涨
        #   （同条件周瑜正常 45）。配置才是权威。
        _cb = growth.hero_can_break(_h.get("type"))
        if bool(_h.get("can_break")) != bool(_cb):
            _h["can_break"] = _cb
            _changed = True
    # 升级符兜底：老存档里没有就往里补
    _items = PLAYER_DATA.setdefault("items", {})
    for _iid, _cnt in (("20005", 999), ("20006", 999), ("20007", 999)):
        if int(_items.get(_iid, 0)) < 100:
            _items[_iid] = _cnt
            _changed = True
    # 缘分卡（替身卡）兜底：曹操/曹仁/郭嘉/贾诩 的 4·5·6 星
    for _iid in ("25000", "25001", "25002", "25009", "25010", "25011",
                 "25069", "25070", "25071", "25021", "25022", "25023"):
        if int(_items.get(_iid, 0)) < 50:
            _items[_iid] = 200
            _changed = True
    # 装备兜底：4 件神装，赤兔默认穿在曹操 6★(实例 5) 身上
    _eq = PLAYER_DATA.setdefault("equips", {})
    _seed_equips = {
        "1": {"id": 1, "type": 33115, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
        "2": {"id": 2, "type": 33415, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
        "3": {"id": 3, "type": 33315, "host": 0, "strengthen": 0, "grade": 0, "grade_exp": 0},
        "4": {"id": 4, "type": 33215, "host": 5, "strengthen": 0, "grade": 0, "grade_exp": 0},
    }
    for _k, _v in _seed_equips.items():
        if _k not in _eq:
            _eq[_k] = dict(_v)
            _changed = True
    for _hid, _h in PLAYER_DATA.get("heroes", {}).items():
        _h.setdefault("equips", [])
    for _ek, _ev in _eq.items():
        _host = int(_ev.get("host") or 0)
        if _host > 0:
            _hh = PLAYER_DATA.get("heroes", {}).get(str(_host))
            if _hh is not None and _ev["id"] not in _hh.get("equips", []):
                _hh.setdefault("equips", []).append(_ev["id"])
                _changed = True
    if not any(h.get("beast") or growth.is_beast(h.get("type")) for h in _heroes.values()):
        _top = max([int(k) for k in _heroes] or [0])
        _hero_cfg = game_config.load_hero_config()
        for _base in (14001, 14011, 14021, 14031):
            for _star in range(1, 6):
                _bid = _base + _star - 1
                if _bid not in _hero_cfg:
                    continue
                _lv = growth.beast_lv(_star)
                _st = hero_attrs(_bid, _lv, 0)
                try:
                    _av = game_config.get_hero_avatar(_bid)
                    _si = game_config.get_hero_star_info(_bid)
                except Exception:
                    _av, _si = {"spine": "", "avatar": ""}, {"avatar_type": 0}
                for _ in range(3 if _star in (4, 5) else 1):
                    _top += 1
                    _heroes[str(_top)] = {
                        "id": _top, "type": _bid, "lv": _lv,
                        "grade": 0, "star": _star, "exp": 0, "awaken": 0,
                        "can_break": False, "beast": True,
                        "maxHP": _st.get("maxHP", 0), "attack": _st.get("attack", 0),
                        "defence": _st.get("defence", 0), "speed": _st.get("speed", 0),
                        "comboRate": 0, "blockRate": 0, "counterRate": 0,
                        "critRate": 0, "critDamage": 150, "position": 0,
                        "name": game_config.get_hero_name(_bid),
                        "avatar_type": _si.get("avatar_type", 0),
                        "spine": _av.get("spine", ""), "avatar": _av.get("avatar", ""),
                    }
        _changed = True
        print("[migrate] 已补入圣兽材料 %d 个" % _top)
    # 升星前置是"满级 + 满阶(9)"，而升级/升阶协议尚未实现，
    # 所以把曹操 4★/5★ 的第一个实例顶到满级满阶，保证升星页面开箱可测。
    # 不想要就设 _SEED_STARUP_READY = False 并删掉 hero 里的 _starup_ready 标记。
    if _SEED_STARUP_READY:
        for _t, _q in ((10014, 4), (10015, 5)):
            for _v in _heroes.values():
                if _v.get("type") == _t and not _v.get("beast"):
                    if _v.get("_starup_ready"):
                        break
                    _v["lv"] = growth.star_max_lv(_q)
                    _v["grade"] = growth.MAX_GRADE
                    _v["_starup_ready"] = True
                    print("[migrate] 升星测试夹具：实例#%s(%s) 置为 Lv%s %s阶" % (
                        _v["id"], _v.get("name"), _v["lv"], _v["grade"]))
                    _changed = True
                    break
    if _changed:
        save_player_data(PLAYER_DATA)

HTTP_PORT = 8000
TCP_PORT = 2724
SERVER_ID = 10001

# 文件日志（console 窗口看不到排障信息，双写一份到文件）
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_server.log")
_log_fp = open(_LOG_PATH, "a", buffering=1, encoding="utf-8")

def flog(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    _log_fp.write(line + "\n")

MAGIC = 10008
OP_LOGIN_OK = 100
OP_ENTER_GAME = 102
OP_SELECT_CHARACTER = 10001
OP_SET_GUIDE_INDEX = 10101
OP_LEVEL_BEGIN = 5030
OP_LEVEL_END = 5031
# ★ 战报（2026-10-29）：结算面板的「战报」按钮 = sendToReviewAttack(battleID)
#   → socket:send("ShowBattle", 5070, {key=battleID}) → 回包按 **AttackArena** 解码
#   → battleRecordParams.result = AttackArena → BattleRecordPanel 读 result.battleRecord。
#   所以 LevelEnd 必须回 battleID（LevelResult.7），5070 必须把上传的 BattleRecordData
#   原样吐回去，否则面板拿不到记录 → 统计全空。
OP_SHOW_BATTLE = 5070
OP_SWEEP = 5032            # 扫荡：IDCountMessage{id=关卡, count=次数} -> LevelResults
OP_BUY_LEVEL_TIMES = 6004  # 重置关卡次数：IDMessage{id=关卡} -> IDCountMessage{id,count}
# ---- 挑战副本（2026-10-29，见 game/challenge.py）----
# 圣兽岛(7001~7012)/金银岛(80001~80005) 走通用 5030/5031；
# 阵营副本(90030~90064) 走 6007/6008（请求体同样是关卡 ID）。
OP_THERION_INFO = 5034             # GetTherionInfo -> NewTherionInfo（圣兽 4 岛次数/开岛）
OP_CHALLENGE_INFO = 5037           # GetChallengeInfo -> ChallengeInfo（挑战面板左上角次数）
OP_ACTIVITY_LEVEL_GROUP = 5327     # GetActivityLevelGroup -> ActivityLevelGroupData
OP_ACTIVITY_LEVEL_GROUP_PREVIEW = 5329  # NextActivityLevelGroup（预览下一轮）
OP_ACTIVITY_LEVEL_BEGIN = 6007     # ActivityLevelBegin（阵营副本开战）-> LevelBegin
OP_ACTIVITY_LEVEL_END = 6008       # ActivityLevelEnd（阵营副本结算）-> LevelResult
OP_CHALLENGE_SWEEP = 6009          # DailyChallengeSweep（VIP 挑战扫荡）-> AwardResult
OP_EQUIP_HERO = 20412    # 上阵/调整位置
OP_UNEQUIP_HERO = 20413  # 下阵
OP_AWAKEN_HERO = 5023    # 突破：PropActionMessage{id, targetID} -> HeroData
OP_EVOLVE_HERO = 5024    # 升星：PropActionMessage{id, targetID} -> HeroData
OP_GRADE_HERO_LIST = 5028  # 升阶请求：IDListMessage{id=[武将, 材料...]} -> HeroData
# ★ 升阶请求/响应 opcode 不对称（2026-10-21 实锤）：客户端 reqShengjie 发 5028
#   （GradeHeroByList），但 ackShengjie 只注册在 5021（GradeHeroRt）上；5028
#   （GradeHeroByListRt）全客户端代码库没有任何 handler —— 响应挂 5028 会被
#   SocketHandler 直接丢弃（debug.log: "没有解析的谷歌协议: opCode = 5028"），
#   界面永不刷新。响应必须挂 5021。
OP_GRADE_HERO_RT = 5021    # 升阶响应（GradeHeroRt）：HeroData
OP_ADVANCE_EXP = 5027      # 一键提炼（消耗经验符升级）：IDMessage{id} -> HeroData
OP_USE_ACTIVATION_CODE = 8000  # 激活码输入框 → 改造为 GM 命令台（2026-10-22）
OP_DELETE_ACCOUNT = 8001   # 设置-注销账号：删当前账号及全部角色/存档（2026-10-31）
OP_USE_FUNCTION = 5008     # 点具体某个符：FunctionData{id=物品, name, count, param=武将id}
OP_UPDATE_HERO = 453       # 单武将数据推送：HeroData（用符/升阶后让客户端刷新等级）
OP_FASTEN_FATE = 20300     # 强化缘分：IDListMessage{id={武将实例, 材料}} -> FateFastenInfo
OP_UPDATE_CHARACTER = 200  # 玩家数据推送：UpdateCharacter{character=CharacterData}（战力/货币）
OP_SQUAD_POWER = 20416     # 上阵队伍战力变化：BattleGameplayPowerChange{category,index,power}
OP_DELETE_PROP = 499       # 删除道具/武将：DeleteData{typeID=1(武将)/其他, id}
OP_WEAR_EQUIP = 5012       # 穿戴装备：PropActionMessage{id=装备, targetID=武将} -> EquipData
OP_DOWN_EQUIP = 5013       # 卸下装备：IDMessage{id=装备} -> EquipData
OP_STRENGTHEN = 5014       # 强化一次：IDMessage{id} -> EquipData
OP_STRENGTHEN_AUTO = 5016  # 批量强化**请求**（客户端发 5016）
OP_STRENGTHEN_AUTO_RT = 5015  # 批量强化**响应**（客户端 ackStrengthEquipAuto 注册在 5015！）
OP_GRADE_UP_EQUIP = 6021   # 装备升阶：IDListMessage{id=[装备, 材料...]} -> EquipData
OP_STAR_UP_EQUIP = 6023    # 装备升星：IDListMessage{id=[装备, 材料1, 材料2]} -> EquipData
OP_REFORM = 6025           # 改造/洗练副属性 -> EquipData
OP_SAVE_REFORM = 6026      # 保存洗练结果 -> EquipData
OP_GIVEUP_REFORM = 6027    # 放弃洗练结果 -> EquipData
OP_DECOMPOSE = 20210       # 分解装备：IDListMessage{id=[装备...]}
OP_SYNTHETIC = 5020        # 碎片合成：IDMessage{id=碎片道具ID} -> 产出装备
OP_UNLOCK_ASSIST = 6024    # 解锁副属性：IDListMessage{id=[装备, 槽位]}（耗300元宝）
OP_LOTTERY = 5040          # 招募/寻宝：UseLotteryData{id=记录id, useItem} -> 476 或 AwardResult
OP_LOTTERY_RECORDS = 5041  # 抽卡记录：空 body -> LotteryRecordDatas
OP_LOTTERY_ACTIVITY = 5318 # 折扣活动推送：LotteryActivityInfo
OP_COLLECT_AWARD = 10115   # 图鉴收集奖励领取：ReceiveCollectionAwardRequestData -> CollectionAwardRecordData

# ---- 军旗系统（2026-10-23）----
OP_FLAG_PART_PRODUCT = 5346      # 制作部件：MultIDCounteListByIDMessage -> FlagPartData
OP_FLAG_PART_UP = 5347           # 升级部件：MultIDCounteListByIDMessage -> FlagPartData
OP_FLAG_PART_UNLOCK_ADD = 5348   # 解锁词条：IDMessage -> FlagPartData
OP_FLAG_PART_RESOLVE = 5349      # 分解部件：IDMessage -> IDListMessage
OP_EQUIP_FLAG_PART = 5351        # 装备部件到旗帐：PropActionMessage -> FlagPartData
OP_UNEQUIP_FLAG_PART = 5352      # 卸下部件：IDMessage -> FlagPartData
OP_EQUIP_FLAG = 5353             # 军旗上阵：EquipFlagToBattleGameplayIndexRequest -> UpdateBattleGameplaySquadDatas
OP_UNEQUIP_FLAG = 5354           # 军旗下阵：UnequipFlagFromBattleGameplayIndexRequest -> UpdateBattleGameplaySquadData
OP_UNLOCK_NEW_FLAG = 5356        # 解锁旗帐：无 body -> FlagData
OP_UPDATE_WARBOOK = 5371         # 跑商记录推送：WarBookRecordData
OP_CHOOSE_FLAG_DRAW_MAP = 31204  # 跑商选图：IDListMessage{id=[mapId]} -> IDListMessage
OP_GIVEUP_FLAG_DRAW = 31205      # 放弃跑商：无 body -> WarBookRecordData（货物留一半）
OP_GET_FLAG_DRAW_REWARD = 31206  # 跑商结算：无 body -> WarBookRecordData（货物入包+经验）
OP_START_FLAG_DRAW = 31207       # 开跑：无 body -> WarBookRecordData（耗门票）
OP_DIVINATION = 31203            # 前进/遇商人：无 body -> DivinationData（扣铜币掷骰）
OP_DIVINATION_EVENT = 31208      # 领取商人事件：无 body -> DivinationResultData
OP_REMOVE_MAP_REDPOINT = 31209   # 移除地图红点：IDListMessage -> IDListMessage
OP_BLUEPRINT_SYNTH = 5355        # 图纸合成：IDCounteListByIDMessage{id=配置ID,idCount=[{id=图纸,count}]} -> AwardResult
OP_BUY_COIN = 10050            # 买铜钱：无 body -> 裸 CharacterData
OP_BUY_EP = 10051              # 买体力：无 body -> 裸 CharacterData
OP_COIN_CRIT = 10053           # 买铜钱暴击通知：CoinCritRateInfo{rate, coin, rateCoin}
OP_NEW_MISSION = 501           # 新任务：MissionData
OP_UPDATE_MISSION = 502        # 任务进度更新：MissionData
OP_DELETE_MISSION = 503        # 删除任务：IDMessage{id}
OP_MISSION_SUCCESS = 504       # 领奖结果：MissionSuccess{id, type, AwardResult}
OP_SUBMIT_MISSION = 10100      # 提交/领取任务：IDMessage{id=任务实例id}
OP_CHANGE_NAME = 104           # 改名：StringMessage{msg=新名} -> ChangeNameData{result,newName}
OP_CHANGE_HONOR = 107          # 换头像/头像框：IDMessage{id=honor配置id} -> IDMessage{id}
OP_NEW_MAIL = 300              # 新邮件推送：MailData
OP_NEW_BUY_PROP_RECORD = 590     # 月卡/周卡新购买记录：BuyPropRecordData
OP_UPDATE_BUY_PROP_RECORD = 591  # 月卡/周卡剩余天数刷新：BuyPropRecordData
OP_READ_MAIL = 5003            # 标记已读：IDMessage{id} -> IDMessage{id}
OP_DELETE_MAIL = 5004          # 删除邮件：IDMessage{id} -> IDMessage{id}
OP_GET_MAIL_AWARD = 5005       # 单封领奖：IDMessage{id} -> IDMessage{id}
OP_GET_ALL_MAIL_AWARD = 5006   # 一键领取：无 body -> 空包（客户端不解析）
OP_CHANGE_ACCOUNT = 105        # 换账号（不是改名）：ChangeAccountRequest -> ChangeAccountData

# 关卡进度（原版架构：服务器是关卡进度的 source of truth）
# - 登录时 EnterGame.levelRecords(字段9) 下发通关记录（LevelRecordData）
# - 战斗结算 5031 上报 -> 服务器更新进度 -> 推 520 NewLevelRecord 增量更新
# - 客户端解锁链：LevelConfig.LastLevel（1002->1001, 1003->1002, ...）+ 关卡列表按 ID
#   顺序找"第一个无记录的关"作为可打上限，故 1001 必须有记录，否则后续全锁
# - 1001 是新手引导关（原版由引导强制顺序打通），这里默认已通关（3 星）
# 副本推关记录：**必须挂在 PLAYER_DATA 里持久化**。
# 之前是模块级全局 + 硬编码，服务端一重启推关进度就回退（真 bug）。
LEVEL_RECORDS = PLAYER_DATA.setdefault("levels", {})
LEVEL_RECORDS.setdefault(1001, {"star": 3, "leftTimes": 10, "buyTimes": 0})

# ---------------------------------------------------------------- protobuf 编码
def _varint(n):
    if n < 0:                       # int64 负数用 64 位补码
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field, wire):
    return _varint((field << 3) | wire)


def f_int(field, v):
    """变长整型（int32/int64/bool/enum）"""
    return _tag(field, 0) + _varint(int(v))


def f_bytes(field, b):
    return _tag(field, 2) + _varint(len(b)) + b


def f_str(field, s):
    return f_bytes(field, s.encode("utf-8"))


def f_msg(field, m):
    return f_bytes(field, m)


def _read_varint(buf, pos):
    shift = 0
    result = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _read_string_field(buf, field):
    """读 length-delimited 字符串字段（FunctionData.name / param 用）。"""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        f, wire = key >> 3, key & 7
        if wire == 0:
            _, pos = _read_varint(buf, pos)
        elif wire == 2:
            ln, pos = _read_varint(buf, pos)
            if f == field:
                return buf[pos:pos + ln].decode("utf-8", "replace")
            pos += ln
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            break
    return None


def _read_varint_field(buf, field):
    """取某个 varint 字段的首个值（够调试用）"""
    vals = _read_varint_fields(buf, field)
    return vals[0] if vals else None


def _read_varint_fields(buf, field):
    """读 repeated varint 字段的全部值（升阶 IDListMessage.id 会重复出现）。"""
    out = []
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        f, wire = key >> 3, key & 7
        if wire == 0:
            v, pos = _read_varint(buf, pos)
            if f == field:
                out.append(v)
        elif wire == 2:
            ln, pos = _read_varint(buf, pos)
            if f == field:                      # packed repeated
                end = pos + ln
                while pos < end:
                    v, pos = _read_varint(buf, pos)
                    out.append(v)
            else:
                pos += ln
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            break
    return out


# ---------------------------------------------------------------- 数据构造
CHAR_ID = 1001
CHAR_NAME = "本地测试"      # 兜底；真实名字走 PLAYER_DATA["player"]["name"]，见 char_name()

# ---- 账号隔离（2026-10-29）----
OP_CREATE_CHARACTER = 10000     # CreateCharacter{nickName,gender,type}
OP_CREATE_CHARACTER_OK = 101    # CreateCharacterOK{id,name,gender,type,lv,teamExp}


def apply_player_data(data: dict, char_id: int):
    """把某角色的存档接管为当前全局（切角色用）。

    ★ 必须**原地** clear+update：其他模块持有的是 core.PLAYER_DATA 这个 dict
    对象的引用（属性访问），换对象会让它们全部读到旧数据。
    LEVEL_RECORDS 是 PLAYER_DATA["levels"] 的别名，必须一并重绑。

    ★ data is PLAYER_DATA 时必须跳过 clear+update（2026-10-30）：store 现在
    原地切换存档，switch_player 返回的就是 PLAYER_DATA 本身，再 clear 一次
    会把刚载入的数据清成空 dict（player 键直接消失）。
    """
    global LEVEL_RECORDS, CHAR_ID
    if data is not PLAYER_DATA:
        PLAYER_DATA.clear()
        PLAYER_DATA.update(data)
    LEVEL_RECORDS = PLAYER_DATA.setdefault("levels", {})
    CHAR_ID = int(char_id)
    _BATTLE_RECORDS.clear()          # 战报缓存按角色隔离，切角色即清


def compute_character_brief(data: dict, char_id: int) -> dict:
    """算某角色的 LoginCharacterData 摘要（id/name/lv/power），不动全局。

    同步、无 await —— 事件循环内原子。战力要过 ensign/fate 全链路（深耦合
    PLAYER_DATA），所以用「交换-计算-还原」：先浅拷贝现场，换入目标数据算完
    team_power 再原样还原（含 LEVEL_RECORDS 别名重绑）。
    """
    global LEVEL_RECORDS
    old = dict(PLAYER_DATA)
    try:
        PLAYER_DATA.clear()
        PLAYER_DATA.update(data)
        LEVEL_RECORDS = PLAYER_DATA.setdefault("levels", {})
        p = data.get("player") or {}
        return {"id": int(char_id),
                "name": str(p.get("name") or CHAR_NAME),
                "lv": int(p.get("lv") or 1),
                "power": int(team_power())}
    finally:
        PLAYER_DATA.clear()
        PLAYER_DATA.update(old)
        LEVEL_RECORDS = PLAYER_DATA.setdefault("levels", {})


def build_new_player_data(nick: str, gender: int = 1, ctype: int = 1) -> dict:
    """新角色初始存档（2026-10-30 用户拍板的开局包，替换旧 60 级满配种子）。

    · 队伍等级 1，钱包起步（铜钱 1w / 元宝 100 / 体力补到 1 级上限）
    · 武将不直接发——发卡：五星神将卡(29966)×2 + 五星武将卡(29804)×3（卡实例）
    · 圣兽：玄武/青龙/朱雀/白虎 各 1 只 5 星（14005/14015/14025/14035）
    · 装备：武/盔/甲/宝 各 1 件 3 星神装（33513/33813/33613/33713）
    · 军旗：白色基础组件 2 件（41001 基础旗冠 + 41002 基础旗面）
    · 关卡进度全空（真·从零）
    """
    from . import growth, ensign
    data = {
        "player": {"name": nick, "gender": int(gender), "type": int(ctype),
                   "lv": 1, "exp": 0, "coin": 10000, "diamond": 100,
                   "ep": 30, "vigour": 100},
        "heroes": {}, "equips": {}, "items": {}, "fate": {},
        "levels": {}, "level_records": {},
        "item_cards": {}, "item_card_seq": 0,
        "flag_parts": {}, "flags": {},
    }
    try:
        data["player"]["ep"] = int(
            game_config.load_exp_set_config().get(1, {}).get("MaxEp") or 30)
    except Exception:
        pass

    # ---- 圣兽 5 星 ×4（升星材料实例，不吃经验：lv=该星级固定上限）----
    for hid, bid in enumerate((14005, 14015, 14025, 14035), start=1):
        st = hero_attrs(bid, growth.beast_lv(5), 0)
        star_info = game_config.get_hero_star_info(bid)
        av = game_config.get_hero_avatar(bid)
        data["heroes"][str(hid)] = {
            "id": hid, "type": bid, "lv": growth.beast_lv(5),
            "grade": 0, "star": 5, "exp": 0, "awaken": 0,
            "can_break": False, "beast": True,
            "maxHP": st.get("maxHP", 0), "attack": st.get("attack", 0),
            "defence": st.get("defence", 0), "speed": st.get("speed", 0),
            "comboRate": 0, "blockRate": 0, "counterRate": 0,
            "critRate": 0, "critDamage": 150,
            "position": 0,
            "name": game_config.get_hero_name(bid),
            "avatar_type": star_info.get("avatar_type", 0),
            "spine": av.get("spine", ""), "avatar": av.get("avatar", ""),
        }

    # ---- 3 星神装备 ×4 部位（Quality=5=神，Star=3；武/盔/甲/宝）----
    for eid, etype in enumerate((33513, 33813, 33613, 33713), start=1):
        data["equips"][str(eid)] = {"id": eid, "type": etype, "host": 0,
                                    "strengthen": 0, "grade": 0, "grade_exp": 0}

    # ---- 武将卡实例（Unit=1 卡每张唯一 id，uid 基址 9000000）----
    for uid, ctype_id in enumerate((29966, 29966, 29804, 29804, 29804), start=1):
        data["item_cards"][str(9000000 + uid)] = ctype_id
    data["item_card_seq"] = 5

    # ---- 军旗白色（Q=1）基础组件 ×2：基础旗冠/基础旗面 ----
    for pid, ptype in enumerate((41001, 41002), start=50001):
        cfg = game_config.flag_part_cfg(ptype)
        additions = ensign.build_additions(cfg) if cfg else []
        data["flag_parts"][str(pid)] = {
            "id": pid, "type": ptype, "host": ensign.HOST_NONE,
            "unlock_count": len(additions), "up_count": 0,
            "additions": additions,
        }
    return data


def char_name():
    """当前角色名（改名后立刻生效）。

    builders / legacy 都必须走这里：`CHAR_NAME` 是模块级常量，改名改不动它，
    会导致 CharacterData 一直下发旧名字。
    """
    nm = str((PLAYER_DATA.get("player") or {}).get("name") or "").strip()
    return nm or CHAR_NAME


def name_card_count():
    """改名卡数量（CharacterData 字段 35，客户端改名按钮据此判断能否改名）。"""
    return int((PLAYER_DATA.get("player") or {}).get("change_name_card") or 0)

def _name_taken(name, exclude_id=None):
    """名字是否已被其他玩家占用（查 players 表，单机也只有一行，未来多角色直接用）。"""
    import sqlite3
    try:
        from .. import settings
        con = sqlite3.connect(settings.DB_PATH, timeout=2)
    except Exception:
        return False
    try:
        if exclude_id:
            row = con.execute("select 1 from players where name=? and id<>? limit 1",
                              (str(name), int(exclude_id))).fetchone()
        else:
            row = con.execute("select 1 from players where name=? limit 1",
                              (str(name),)).fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass


# 改名结果码 —— 严格对齐客户端 `UserManager:ackcheckChangeName`
#   1 成功 / 2 存在重复的名字 / 3 名字格式不正确 / 4 改名卡不足 / 其它 = 改名失败
NAME_OK, NAME_DUP, NAME_BADFMT, NAME_NO_CARD = 1, 2, 3, 4


def do_change_honor(honor_id):
    """换头像 / 头像框（107）。返回 (ok, reason, kind)。

    HonorConfig.HonorPart：0=头像（50014+）1=头像框（50000+）。
    客户端 `HeadSelectedPanel:changeHonor` 发 IDMessage{id}，回包同 id 才刷新列表；
    `honorManager:getUserHead()` 读的是 `me:head().type`（CharacterData.characterHead），
    所以改完必须推 200 才能让界面立刻换。
    """
    from . import game_config
    hid = int(honor_id or 0)
    if hid <= 0:
        return False, "参数错误", ""
    row = (game_config.load_honor_config() or {}).get(hid) or {}
    if not row:
        return False, "该头像/头像框不存在", ""
    p = PLAYER_DATA.setdefault("player", {})
    try:
        part = int(float(row.get("HonorPart") or 0))
    except (TypeError, ValueError):
        part = 0
    if part == 1:
        p["head_frame_id"] = hid
        return True, "", "头像框"
    p["head_id"] = hid
    return True, "", "头像"


def do_change_name(new_name):
    """改名：校验 → 扣改名卡 → 落库。返回 (ok, code, reason)。

    规则（用户 2026-10-26）：必须有改名卡、改一次消耗一张、名字不允许重复。
    code 直接喂给 ChangeNameData.result（客户端按 1/2/3/4 弹不同文案）。
    """
    nm = str(new_name or "").strip()
    if not nm:
        return False, NAME_BADFMT, "名字不能为空"
    if len(nm) < 2 or len(nm) > 12:
        return False, NAME_BADFMT, "名字长度需 2~12 个字符"
    if any(ch in nm for ch in "\n\r\t<>/\\|"):
        return False, NAME_BADFMT, "名字含非法字符"
    if nm == char_name():
        return False, NAME_DUP, "新名字与当前名字相同"
    if name_card_count() <= 0:
        return False, NAME_NO_CARD, "改名卡道具不足"
    if _name_taken(nm, CHAR_ID):
        return False, NAME_DUP, "该名字已被占用，请换一个"
    p = PLAYER_DATA.setdefault("player", {})
    p["name"] = nm
    p["change_name_card"] = max(0, name_card_count() - 1)
    return True, NAME_OK, "ok"


def build_honors():
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_honors()


def build_head_type():
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_head_type()


def build_login_character_data():
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_login_character_data()


def build_login_ok(characters: list | None = None):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）

    characters：账号隔离后 session 传入的角色列表（可选，None 走老路径）。
    """
    from . import builders_pb
    return builders_pb.build_login_ok(characters)


def build_coin_crit_info(rate, coin, rate_coin):
    from . import builders_pb
    return builders_pb.build_coin_crit_info(rate, coin, rate_coin)


def build_buy_character_body():
    from . import builders_pb
    return builders_pb.build_buy_character_body()


def build_mission_data(m):
    from . import builders_pb
    return builders_pb.build_mission_data(m)


def build_mission_success(mid, mtype, award_body):
    from . import builders_pb
    return builders_pb.build_mission_success(mid, mtype, award_body)


def mission_snapshot():
    """EnterGame 用：(missions 列表, completed 配置 id 列表)。

    ★ 2026-10-29：除了主线，还要把**每日扫荡令牌任务**塞进这里，不能只靠 501 推。
      原因（客户端 UserManager/MissionManager 实读）：EnterGame 的任务列表是
      **延迟处理**的 —— `initDataOnLogin()` 先 `_missionList = {}` 再灌 EnterGame
      自带的 missions。我们原本登录后单独推 501，帧顺序虽在 102 之后，但
      102 的回调链执行得更晚，501 加进去的条目随即被这次清空抹掉，
      表现为「限时任务里看不到每日令牌任务」（客户端日志里 501 解析到了却没有格子）。
    """
    from . import mission
    missions, completed = mission.snapshot()
    try:
        # 成就/收集类任务（星星收集狂/等级礼包/大师之路…，2026-10-29）
        missions.extend(mission_achieve.snapshot_missions())
    except Exception as _e:
        flog("[mission] 成就任务快照失败: %r" % _e)
    try:
        # 周期任务（每日击杀/BOSS、每周获得武将、每月合成、三餐体力，2026-10-29）
        missions.extend(mission_periodic.snapshot_missions())
    except Exception as _e:
        flog("[mission] 周期任务快照失败: %r" % _e)
    try:
        row = daily_token.pending()
        if row and not any(int(m.get("id") or 0) == int(row.get("id")) for m in missions):
            st = daily_token.state()
            accept = int(st.get("accept") or 0) or int(time.time())
            st["accept"] = accept
            missions.append({
                "id": int(row.get("id")),
                "type": int(row.get("id")),
                "need": 1,
                "complete": 1,
                "accept": accept,
            })
    except Exception as e:
        flog("[daily_token] 加入 EnterGame 任务列表失败（%r）" % e)
    return missions, completed


def build_mail_data(m):
    """MailData 序列化（顶层函数 —— dispatch 内有局部 `import builders_pb`，
    直接在函数里引用会把整个函数作用域的 builders_pb 变成局部变量，导致
    UnboundLocalError：之前邮件推送全部因此静默失败）。"""
    from . import builders_pb
    return builders_pb.mail_data_msg(m).SerializeToString()


def mail_snapshot():
    """EnterGame 用：未过期且未领取的邮件列表（已领取的已在 claim 时删除）。"""
    from . import mail
    return mail.to_snapshot()


def push_mail(mid):
    """把一封邮件作为 300 NewMail 推给在线客户端，返回是否推送成功。

    客户端 MailManager:onSvrNewMail → addSvrMail + dispatchEvent("NewMail")，
    已打开的邮件面板（MailBoxPanel）会立刻 refresh 显示出来。
    后台发信台 / GM 发信都走这里，否则在线玩家必须重登才能看到新邮件。
    """
    from . import mail
    mid = int(mid or 0)
    online = len(LIVE_SESSIONS)
    for m in mail.to_snapshot():
        if m["id"] == mid:
            broadcast(OP_NEW_MAIL, build_mail_data(m))
            flog("[mail] push_mail #%d -> 300 NewMail（在线会话 %d）" % (mid, online))
            return True
    flog("[mail] push_mail #%d 未在快照中找到（已领取/已过期？）" % mid)
    return False


def buy_prop_records_snapshot():
    """月卡/周卡有效期快照（EnterGame.buyPropRecordDatas 用）。"""
    from . import month_card
    return month_card.records_snapshot()


def push_buy_prop_record(rec, is_new=False):
    """把月卡/周卡记录推给在线客户端：590 新建 / 591 更新（BuyPropRecordData）。

    客户端 UserManager:newBuyPropRecord 追加、updateBuyPropRecord 按 id 覆盖，
    两者都会刷新商城面板，所以跨天结算后必须推 591，否则剩余天数不更新。
    """
    from . import builders_pb
    opcode = OP_NEW_BUY_PROP_RECORD if is_new else OP_UPDATE_BUY_PROP_RECORD
    try:
        broadcast(opcode, builders_pb.buy_prop_record_msg(rec).SerializeToString())
    except Exception as e:
        flog("[month_card] push record 失败（%r）" % e)


def buy_snapshot():
    """购买次数快照（给 builders_pb.character_msg 的 actionTime 用）。"""
    from . import buy
    return buy.snapshot()


def build_character_data():
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_character_data()


def update_character_body():
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.update_character_body()


def build_hero_data(hid, htype, lv=1, pos=-1, stats=None, exp_override=None,
                    extra_bonuses=None):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）

    extra_bonuses：编队级加成（军旗词条）。★ 改 builders 签名必须同步这里——
    dispatch 走的是本函数，签名漏了参数会在 LevelBegin 直接 TypeError。
    """
    from . import builders_pb
    return builders_pb.build_hero_data(hid, htype, lv, pos, stats, exp_override,
                                       extra_bonuses)


def _find_hero(instance_id):
    return PLAYER_DATA.get("heroes", {}).get(str(instance_id))


def _drop_hero(instance_id):
    PLAYER_DATA.get("heroes", {}).pop(str(instance_id), None)


def _refresh_hero_type(hero, new_type):
    """升星后切换武将 type，并同步头像/骨骼/可否突破等派生字段。"""
    hero["type"] = new_type
    try:
        st = game_config.get_hero_star_info(new_type)
        hero["star"] = st.get("quality", hero.get("star", 1))
        hero["avatar_type"] = st.get("avatar_type", hero.get("avatar_type"))
        av = game_config.get_hero_avatar(new_type)
        hero["spine"] = av.get("spine", hero.get("spine"))
        hero["avatar"] = av.get("avatar", hero.get("avatar"))
    except Exception as e:
        print("[warn] refresh_hero_type(%s) 失败: %s" % (new_type, e))
    hero["can_break"] = growth.hero_can_break(new_type)
    # 升星后是新 type → 图鉴点亮（新星级也是一条独立图鉴）
    _record_hero_collect(new_type, hero.get("id"))


def do_star_up(hero, mat):
    """执行升星。返回 (ok, reason)。等级保留（上限随星级提高）。

    阶数：**每次升星都把阶数清零**（4★9阶 → 5★0阶 → 5★9阶 → 6★0阶 → 6★9阶）。
    """
    if not hero or not mat:
        return False, "武将或材料不存在"
    htype = hero.get("type")
    ok, why, need_type, coin = growth.check_star_up(
        htype, hero.get("lv", 1), hero.get("grade", 0))
    if not ok:
        return False, why
    if mat.get("type") != need_type:
        return False, "材料不对（需要 %d，实际 %s）" % (need_type, mat.get("type"))
    if mat.get("id") == hero.get("id"):
        return False, "不能消耗自己"
    if mat.get("position", 0) > 0:
        return False, "阵位上的武将不能作为材料"
    if PLAYER_DATA["player"].get("coin", 0) < coin:
        return False, "金币不足（需要 %d）" % coin

    PLAYER_DATA["player"]["coin"] -= coin
    _drop_hero(mat["id"])
    _refresh_hero_type(hero, growth.next_type(htype))
    # 每次升星都清阶：新星级的阶要重新爬
    hero["grade"] = 0
    hero["grade_exp"] = 0
    return True, ""


def do_grade_up(hero, mats):
    """执行升阶：累加每颗材料提供的进度，满 100 点进 1 阶，上限 9 阶。

    进度公式与 WarriorData:getGainGradeExp() 一致；材料星级必须 <= 武将星级。
    铜币 = ExpSetConfig[品质].UpGradeExpToCoin × 本次材料总进度
    （客户端 WarriorShengjiePanel:caculateCoinCost 同式，且按钮前置检查
    `totalCost > me:coin()` → showBuyCoinAlert；2026-10-28 用户报障
    「武将升阶没有消耗铜钱吗」→ 补扣费）。
    """
    if not hero:
        return False, "武将不存在"
    if not mats:
        return False, "未选择材料"
    htype = hero.get("type")
    row = growth.find_hero_row(htype)
    if not row:
        return False, "配置缺失"
    q = int(growth._num(row.get("Quality")))
    if growth._num(row.get("CanUpgrade")) <= 0:
        return False, "该武将无法升阶"
    grade = int(hero.get("grade") or 0)
    if grade >= growth.MAX_GRADE:
        return False, "武将已满阶（9阶）"

    gain = 0
    for m in mats:
        if m.get("id") == hero.get("id"):
            return False, "不能消耗自己"
        if m.get("position", 0) > 0:
            return False, "阵位上的武将不能作为材料"
        mrow = growth.find_hero_row(m.get("type"))
        mq = int(growth._num(mrow.get("Quality"))) if mrow else 0
        if mq > q:
            return False, "材料星级 %d 高于武将星级 %d" % (mq, q)
        gain += growth.grade_exp_gain(q, mq, m.get("grade", 0), m.get("grade_exp", 0))
    if gain <= 0:
        return False, "材料提供的升阶进度为 0"

    # ---- 铜币校验（先算后扣，被拒不耗材料不扣币）----
    total_cost = growth.hero_grade_coin_cost(q, gain)
    coin = int(PLAYER_DATA.get("player", {}).get("coin") or 0)
    if coin < total_cost:
        return False, "铜币不足（需 %d，有 %d）" % (total_cost, coin)

    ng, nge = growth.add_grade_exp(grade, hero.get("grade_exp", 0), gain)
    for m in mats:
        _drop_hero(m["id"])
    hero["grade"], hero["grade_exp"] = ng, nge
    PLAYER_DATA["player"]["coin"] = coin - total_cost
    return True, "+%d 进度 -> %d阶 %d点（耗币%d）" % (gain, ng, nge, total_cost)


def do_awaken(hero, mat):
    """执行突破。返回 (ok, reason)。材料 = 同名同星级（6★）卡，等级不限。"""
    if not hero or not mat:
        return False, "武将或材料不存在"
    htype = hero.get("type")
    awaken = int(hero.get("awaken") or 0)
    ok, why = growth.can_awaken(
        growth.find_hero_row(htype).get("Quality"),
        hero.get("lv", 1), awaken,
        growth.hero_can_break(htype))
    if not ok:
        return False, why
    if mat.get("type") != htype:
        return False, "材料必须是同名同星级卡"
    if mat.get("id") == hero.get("id"):
        return False, "不能消耗自己"
    info = growth.awaken_info(int(float(growth.find_hero_row(htype).get("Quality"))),
                             hero.get("lv", 1))
    coin = info["coin"] if info else 0
    if PLAYER_DATA["player"].get("coin", 0) < coin:
        return False, "金币不足（需要 %d）" % coin

    PLAYER_DATA["player"]["coin"] -= coin
    _drop_hero(mat["id"])
    hero["awaken"] = awaken + 1
    return True, ""


def do_advance_exp(hero):
    """一键提炼（客户端「元宝一键升级」）：直接花元宝把武将升到当前上限。

    ★ 2026-10-28 修（用户报障「只能升到 39 级 100%」）：
      旧实现只吃背包里的免费经验符、不扣元宝——背包符耗尽后一键永远
      「经验符不足」被拒，表现就是卡在当前等级不动。
      客户端口径（WarriorData:getTufeiCost）是**纯元宝购买**：总经验按贪心
      折成 1000点符×100元宝 / 100点符×10元宝 / 10点符×1元宝，确认后发
      5027（不带数量），服务端应扣元宝并直接加经验。
      最小面额向上取整（对齐 getTufeiCost 的 math.ceil），多买的 ≤9 点
      经验到上限后按原包规则作废。

    客户端 reqTufei 只发武将 id。
    """
    if not hero:
        return False, "武将不存在"
    htype = hero.get("type")
    row = growth.find_hero_row(htype)
    if not row:
        return False, "配置缺失"
    q = int(growth._num(row.get("Quality")))
    lv = int(hero.get("lv") or 1)
    cap = growth.level_cap(q, hero.get("awaken", 0), growth.hero_can_break(hero.get("type")))
    if lv >= cap:
        return False, "武将已满级（%d）" % cap

    need = growth.exp_to_cap(lv, hero.get("exp", 0), cap, q)
    if need <= 0:
        return False, "无需升级"
    a, rem = divmod(need, 1000)          # 天灵符 1000点
    b, rem = divmod(rem, 100)            # 鬼才符 100点
    c = (rem + 9) // 10                  # 训练符 10点（向上取整）
    cost = 100 * a + 10 * b + c
    gained = a * 1000 + b * 100 + c * 10
    if not spend_diamond(cost, "一键提炼"):
        p = PLAYER_DATA.get("player", {})
        return False, "元宝不足（需 %d，现有 %d）" % (cost, int(p.get("diamond") or 0))

    nl, ne, ups, waste = growth.add_exp(q, lv, hero.get("exp", 0), gained, cap, growth.hero_can_break(htype))
    hero["lv"], hero["exp"] = nl, ne
    extra = ("，溢出%d点作废" % waste) if waste else ""
    return True, "耗元宝%d -> Lv%d exp%d（升%d级%s）" % (cost, nl, ne, ups, extra)




# ============ 概率礼包 / 自选箱 / 选将卡（2026-10-23 完整实现）============

def _item_groups_pick(icfg, rng=None):
    """道具层 AwardGroup 按权重选一组 → 返回该组的池 id 列表。"""
    import random as _r
    rng = rng or _r
    groups = [g for g in (icfg.get("groups") or []) if g.get("pool_ids")]
    if not groups:
        return list(icfg.get("pool_ids") or [])
    total = sum(max(1, int(g["weight"])) for g in groups)
    pick = rng.random() * total
    acc = 0
    for g in groups:
        acc += max(1, int(g["weight"]))
        if pick < acc:
            return list(g["pool_ids"])
    return list(groups[-1]["pool_ids"])


def roll_gift_awards(item_id, count=1):
    """开箱掷骰：count 次独立抽取 → 合并成 [{kind,type,count}]。"""
    from . import game_config, lottery_config
    icfg = game_config.get_item(item_id)
    if not icfg:
        return None, "道具配置不存在"
    if icfg.get("func") != "SendAward":
        return None, "不是可打开的礼包类道具"
    # ★ 武将/装备**不合并**：每个都是独立实例（同名同星也各一个），
    #   只有可堆叠道具（item）才按 type 合并数量（2026-10-23 用户明确）。
    out = []
    item_merge = {}
    for _ in range(max(1, int(count))):
        picked = _item_groups_pick(icfg)
        if not picked:
            return None, "礼包未配置奖励池"
        for pool_id in picked[:1]:      # 道具层每组通常引用 1 个池
            for a in lottery_config.roll_pool(int(pool_id)):
                if a["kind"] == "item":
                    k = int(a["type"])
                    if game_config.is_card_item(k):
                        # 卡类不可堆叠（原版每张一个卡格）：逐张展开，不合并
                        for _ in range(max(1, int(a["count"]))):
                            out.append({"kind": "item", "type": k, "count": 1})
                    else:
                        item_merge[k] = item_merge.get(k, 0) + int(a["count"])
                else:
                    out.append({"kind": a["kind"], "type": int(a["type"]), "count": 1})
    for t, c in item_merge.items():
        out.append({"kind": "item", "type": t, "count": c})
    return out, ""


# ---- 道具实例模型：武将卡 / 定缘卡（2026-10-27）----
# 原版口径：ItemConfig.Unit=1 → 客户端 canStack=false → 服务端**每张卡一个实例 id**
# （客户端 getRDCardList 不做数量展开，N 张卡 = N 条 ItemData = N 个卡格）。
# 普通道具仍是按 type 堆叠（items[type]=count）；卡类改存 item_cards[uid]=type。
CARD_UID_BASE = 9000000          # 实例 id 起点（与道具 type / 装备实例 id 不冲突）


def item_cards():
    """卡类实例：{str(uid): 道具 type}（count 恒为 1）。"""
    return PLAYER_DATA.setdefault("item_cards", {})


def _next_card_uid():
    seq = int(PLAYER_DATA.get("item_card_seq") or 0) + 1
    PLAYER_DATA["item_card_seq"] = seq
    return CARD_UID_BASE + seq


def card_type_of(uid):
    """实例 id → 卡 type；不是卡实例返回 None。"""
    try:
        return int((PLAYER_DATA.get("item_cards") or {}).get(str(int(uid))) or 0) or None
    except (TypeError, ValueError):
        return None


def resolve_item_type(item_id):
    """请求里的道具 id 解析：卡实例 id → 卡 type；普通道具 id 原样返回。"""
    return card_type_of(item_id) or int(item_id or 0)


def item_count(item_type, data=None):
    """该 type 的持有总数（堆叠数量 + 卡实例张数）。"""
    d = data or PLAYER_DATA
    t = int(item_type or 0)
    n = int((d.get("items") or {}).get(str(t)) or 0)
    if game_config.is_card_item(t):
        n += sum(1 for v in (d.get("item_cards") or {}).values()
                 if int(v or 0) == t)
    return n


def add_item(item_type, count=1):
    """发放道具：卡类按实例入库（每张唯一 id，广播 401 让客户端加卡格），
    普通道具累加堆叠（不发包，调用方自行推 451/401）。

    返回新建的卡实例 id 列表（普通道具返回空列表）。
    """
    t = int(item_type or 0)
    c = int(count or 0)
    if t <= 0 or c <= 0:
        return []
    if game_config.is_card_item(t):
        cards = item_cards()
        created = []
        for _ in range(c):
            uid = _next_card_uid()
            cards[str(uid)] = t
            created.append(uid)
            try:
                broadcast(401, build_item_data(uid, 1, t))
            except Exception as e:
                flog("[item] 广播卡片实例 %s 失败（%r）" % (uid, e))
        return created
    inv = PLAYER_DATA.setdefault("items", {})
    inv[str(t)] = int(inv.get(str(t)) or 0) + c
    return []


def migrate_card_items():
    """启动迁移 / 自愈：

    ① item_cards 里配置已不存在 / 不再是卡类的实例 → 丢弃或并回堆叠；
    ② 旧存档里按 type 堆叠的武将卡/定缘卡（含开包型武将卡）→ 拆成唯一实例。

    返回 (迁出的张数, 并回堆叠的实例数, 丢弃的非法实例数)。
    """
    inv = PLAYER_DATA.setdefault("items", {})
    cards = item_cards()
    restored = 0
    dropped = 0
    for uid in list(cards.keys()):
        t = int(cards.get(uid) or 0)
        if not game_config.item_exists(t):
            cards.pop(uid, None)
            dropped += 1
            continue
        if not game_config.is_card_item(t):
            # 判定口径已放宽到「SubType=1/7 且 Unit<=1」；仍不满足的（配置改动）
            # 并回堆叠，避免实例永远占格
            inv[str(t)] = int(inv.get(str(t)) or 0) + 1
            cards.pop(uid, None)
            restored += 1
    moved = 0
    for k in list(inv.keys()):
        try:
            t = int(k)
        except (TypeError, ValueError):
            continue
        if not game_config.is_card_item(t):
            continue
        cnt = max(0, int(inv.pop(k) or 0))
        for _ in range(cnt):
            uid = _next_card_uid()
            cards[str(uid)] = t
            moved += 1
    return moved, restored, dropped


def push_item_count(session, item_id, count=None):
    """道具数量同步：>0 推 451 UpdateItem；==0 推 499 DeleteProp(typeID=2)。

    ★ 客户端 `BagManager:onUpdateItem` **只改 count、从不删条目**
      （源码里没有 purgeZero，之前注释里的假设是错的）。
      删除走 `onDelProp`：`DeleteData{typeID=2, id=道具id}` → bagManager:removeItem(id)。
      所以道具用光时只推 451(count=0) 的结果是「数量变 0 但条目一直挂在列表里」
      （2026-10-26 用户报障：军旗仓库随机材料箱用完后不消失）。
    """
    item_id = int(item_id)
    # 卡类是实例模型：没有「按 type 的数量」可推，新增实例由 add_item 广播 401、
    # 消耗时调用方按实例 id 推 499（这里直接返回持有总数，不发包）。
    if game_config.is_card_item(item_id):
        return item_count(item_id) if count is None else int(count)
    inv = PLAYER_DATA.get("items") or {}
    n = int(inv.get(str(int(item_id)), 0)) if count is None else int(count)
    if n > 0:
        session.send(451, build_item_data(int(item_id), n))
    else:
        session.send(499, f_int(1, 2) + f_int(2, int(item_id)))
    return n


def do_use_gift(session, item_id, count):
    """使用开箱礼包（SendAward）。返回 (ok, why)。调用方负责 5008 ack。

    ★ 开包型武将卡（五星神将卡、魏国卫军卡等）也是 Unit=1 的卡实例：
      请求里的 item_id 是实例 uid，配置查询与随机池要按 type 取；
      消耗是删除该实例（推 499 删格），而不是减 items 堆叠。
    """
    from . import game_config
    iid = int(item_id or 0)
    inst_type = card_type_of(iid)
    is_card = inst_type is not None
    item_type = inst_type or iid
    inv = PLAYER_DATA.setdefault("items", {})
    icfg = game_config.get_item(item_type) or {}
    # Condition 型（图纸碎片：30 个开 1 次）
    need_per = int(icfg.get("cond_value") or 1) if int(icfg.get("cond_type") or 0) > 0 else 1
    need = max(1, int(count)) * max(1, need_per)
    have = 1 if is_card else int(inv.get(str(item_type), 0))
    if have < need:
        return False, "数量不足（需 %d 个，有 %d 个）" % (need, have)
    awards, why = roll_gift_awards(item_type, count)
    if awards is None:
        return False, why
    if is_card:
        item_cards().pop(str(iid), None)
        session.send(499, f_int(1, 2) + f_int(2, iid))
    else:
        inv[str(item_type)] = have - need
    # 发奖（复用现有分流：道具/装备实例/武将实例/属性）
    _grant_sign_awards(awards)      # 内部已 broadcast(404) 推新武将
    _push_granted_items(session, awards)
    # ★ 不能再调 _push_new_heroes：会重复推 404 → 客户端 addWarriorData
    #   因武将已存在返回 nil → onNewWarrior 里 warrior:getGroup() 崩（2026-10-23）
    push_item_count(session, item_type)
    session.send(301, build_award_result("UseItem", _awards_to_specs(awards)))
    session.send(OP_UPDATE_CHARACTER, update_character_body())
    return True, "开启成功：" + "、".join(
        "%sx%d" % (a.get("type"), a.get("count", 1)) for a in awards)


def do_use_select_item(session, item_id, count, param):
    """自选箱（SelectAnyItem）：param=选中的道具 type。"""
    from . import game_config
    iid = int(item_id or 0)
    inst_type = card_type_of(iid)
    is_card = inst_type is not None
    item_type = inst_type or iid
    inv = PLAYER_DATA.setdefault("items", {})
    icfg = game_config.get_item(item_type) or {}
    choices = [int(x) for x in (icfg.get("items") or [])]
    pick = int(param or 0)
    if pick not in choices:
        return False, "选择无效"
    take = max(1, int(count))
    have = 1 if is_card else int(inv.get(str(item_type), 0))
    if have < take:
        return False, "数量不足"
    if is_card:
        if take > 1:
            return False, "数量不足"
        item_cards().pop(str(iid), None)
        session.send(499, f_int(1, 2) + f_int(2, iid))
    else:
        inv[str(item_type)] = have - take
    # ★ 发奖数量必须 = 消耗的箱子数量：以前写死 count=1，开 10 个箱子只得到 1 个道具
    #   （但箱子扣了 10 个）——2026-10-26 用户报障：八卦自选箱开 10 个图纸，背包只多 1 个。
    awards = [{"kind": "object", "type": pick, "count": take}]
    _grant_sign_awards(awards)
    _push_granted_items(session, awards)
    _push_new_heroes(session, awards)
    if not is_card:
        session.send(451, build_item_data(item_type, int(inv[str(item_type)])))
    # ★ 不发 301：客户端回调里 replacePanel("AwardShow") 自己展示（原版行为）
    session.send(OP_UPDATE_CHARACTER, update_character_body())
    return True, "已选择道具 %d" % pick


def do_use_select_hero(session, item_id, count, param):
    """选将卡（SelectHero）：param=选中的武将 type。
    ★ 404 NewHero 必须在 5008 ack 之前推送（客户端 ack 里 endRecordNewWarrior 取结果）。"""
    from . import game_config
    iid = int(item_id or 0)
    inst_type = card_type_of(iid)
    is_card = inst_type is not None
    item_type = inst_type or iid
    inv = PLAYER_DATA.setdefault("items", {})
    icfg = game_config.get_item(item_type) or {}
    choices = [int(x) for x in (icfg.get("heros") or [])]
    pick = int(param or 0)
    if pick not in choices:
        return False, "选择无效"
    take = max(1, int(count))
    have = 1 if is_card else int(inv.get(str(item_type), 0))
    if have < take:
        return False, "数量不足"
    if is_card:
        if take > 1:
            return False, "数量不足"
        item_cards().pop(str(iid), None)
        session.send(499, f_int(1, 2) + f_int(2, iid))
    else:
        inv[str(item_type)] = have - take
    hid = _create_hero_instance(pick)      # 返回新武将实例 id
    if hid:
        session.send(404, build_hero_data(hid, pick, 1, -1))   # 404 NewHero
    push_item_count(session, item_type)
    # ★ 不发 301：客户端回调里 replacePanel("EmployeeResultPanel") 自己展示
    #   结果武将（SummonWarriorPanel/OptionalItemPanel 原版行为），多发 301
    #   会打乱 replacePanel 链 → 选将面板不关闭 → 焦点卡死（2026-10-23 实测）
    session.send(OP_UPDATE_CHARACTER, update_character_body())
    return True, "已获得武将 %d" % pick


def _push_item_deltas(session, deltas):
    """军旗扣料后的道具同步：>0 推 451、归零推 499 删除（见 push_item_count）。"""
    for t, _n in (deltas or {}).items():
        push_item_count(session, t)


def _push_granted_items(session, awards):
    """发奖后的道具推送：**新 type → 401 NewItem，已有 → 451 UpdateItem**。

    ★ 缺 401 时客户端 onUpdateItem 走 getItem() 取不到 → 直接丢弃，
      新道具要重登才可见（2026-10-23 实测坑）。"""
    inv = PLAYER_DATA.get("items") or {}
    for a in awards:
        if a.get("kind") != "object":
            continue
        t = int(a.get("type") or 0)
        if not game_config.item_exists(t):
            continue
        # 卡类实例已由 add_item 逐张广播 401，这里跳过（按 type 推会推错 id）
        if game_config.is_card_item(t):
            continue
        cnt = int(inv.get(str(t), 0))
        gain = int(a.get("count") or 1)
        # 新增后数量 == 本次发放量 ⇒ 之前为 0 ⇒ 新道具（客户端必须收 401 才入库）
        is_new = (cnt == gain)
        session.send(401 if is_new else 451, build_item_data(t, cnt))


def _push_new_heroes(session, awards):
    """开箱/选将获得的武将实例 → 推 404 NewHero（客户端 onNewWarrior 入库）。

    ★ 2026-10-29 修：**同一实例 id 只能推一次 404**。
      客户端 `WarriorManager:addWarriorData` 遇到已存在的 id 会 `return nil`，
      而 `onNewWarrior` 紧接着 `warrior:getGroup()` → `attempt to index local
      'warrior' (a nil value)` 直接 LUA ERROR，结算面板都出不来（圣兽岛实测）。
      以前 `_grant_sign_awards` 已经逐只 broadcast 过 404，这里再推一次就是重复；
      现在用请求级 seen 集合去重，任一路径都不会重复推。
    """
    heroes = PLAYER_DATA.get("heroes") or {}
    for a in awards:
        if a.get("kind") != "hero":
            continue
        t = int(a.get("type") or 0)
        # 找该 type 的最新实例（刚建的 id 最大）
        cand = [(int(h.get("id") or 0), h) for h in heroes.values()
                if int(h.get("type") or 0) == t]
        if not cand:
            continue
        cand.sort(key=lambda x: x[0])
        hid, h = cand[-1]
        if hid in _HERO_404_SEEN:
            continue            # 本次请求已经推过（_grant_sign_awards 建的）
        _HERO_404_SEEN.add(hid)
        session.send(404, build_hero_data(hid, t, h.get("lv", 1), h.get("position", 0)))
        # ---- 成就刷新：获得武将（target=9 周期 + 终身链）----
        try:
            mission_achieve.push_updates(session)
            mission_periodic.push_refresh(session)
        except Exception as _e:
            flog("[mission] 获得武将刷新失败: %r" % _e)

def do_use_exp_item(hero, item_id, count):
    """经验符使用：**服务端按「刚好升一级」截断张数**（用户 15:20 口径）。

    规则：
      · 最少用 1 张
      · 默认只用「刚好够升一级」的张数（need_count = ceil(还差经验 / 单张面额)）
      · 客户端传的数量大于 need_count 时，只吃 need_count 张（剩余留在背包）
      · 客户端传的数量不足升一级时，**全部用完**（经验累积到 exp，不浪费）
      · 最后一张溢出的经验保留到下一级（由 growth.add_exp 处理）
    """
    if not hero:
        return False, "武将不存在（客户端与服务端数据不一致，请返回主城重新登录）"
    val = growth.exp_items().get(int(item_id or 0))
    if not val:
        return False, "该道具不是经验符（%s）" % item_id
    count = int(count or 0)
    if count <= 0:
        return False, "数量非法"
    inv = PLAYER_DATA.setdefault("items", {})
    have = int(inv.get(str(item_id), 0))
    if have < 1:
        return False, "道具不足（有 %d）" % have

    row = growth.find_hero_row(hero.get("type"))
    q = int(growth._num(row.get("Quality"))) if row else 6
    cap = growth.level_cap(q, hero.get("awaken", 0), growth.hero_can_break(hero.get("type")))
    lv = int(hero.get("lv") or 1)
    if lv >= cap:
        return False, "武将已满级(%d级)，请先升星或突破提升等级上限" % cap

    # ★ 按「刚好升一级」算实际消耗张数
    need_exp = growth.exp_need(lv, q) - int(hero.get("exp") or 0)
    need_cnt = max(1, int((need_exp + val - 1) // val))     # 向上取整
    want = min(count, have)                                  # 客户端请求 & 库存
    actual = max(1, min(want, need_cnt))                     # 最少 1 张，最多刚好升一级
    # 若「刚好升一级」超过库存，则库存全用
    actual = min(actual, have)
    if actual <= 0:
        return False, "道具不足"

    left = have - actual
    if left > 0:
        inv[str(item_id)] = left
    else:
        inv.pop(str(item_id), None)

    gain = val * actual
    nl, ne, ups, waste = growth.add_exp(
        q, lv, hero.get("exp", 0), gain, cap, growth.hero_can_break(hero.get("type")))
    hero["lv"], hero["exp"] = nl, ne
    return True, ("%s×%d张=%d点(需求%d张/只吃%d张) -> Lv%d exp%d（升%d级）"
                  % (item_id, val, gain, need_cnt, actual, nl, ne, ups))


# ---- 配缘（缘分）----
def fate_store():
    return PLAYER_DATA.setdefault("fate", {})


def fate_progress(fate_id):
    """{targetGroup(str): {"exp": int, "complete": 0/1}}"""
    return ((fate_store().get(str(fate_id)) or {}).get("groups")) or {}


def on_field_groups():
    """当前上阵武将的 Group 集合（缘分临时生效判定用）—— 请求级缓存。"""
    hit = _req_cache_get("on_field_groups")
    if hit is not None:
        return hit
    out = set()
    for h in PLAYER_DATA.get("heroes", {}).values():
        if h.get("position", 0) > 0:
            out.add(growth.hero_group(h.get("type")))
    return _req_cache_put("on_field_groups", out)


def hero_equipped_ids(hero):
    """该武将已穿戴装备的 **EquipConfig.Group** 集合。

    `hero["equips"]` 里存的是**装备实例 id**，要经 find_equip() 换成实例，
    再取 type 算 Group（`FateConfig.Group==1` 的 ClientTargets 匹配
    `EquipConfig.Group`，不是实例 id / Item id）。
    """
    out = set()
    for eid in (hero or {}).get("equips", []):
        eq = find_equip(int(eid or 0))
        if eq:
            out.add(growth.equip_group(eq.get("type")))
    return out


# ---------------------------------------------------------------- 装备系统

def equip_store():
    return PLAYER_DATA.setdefault("equips", {})


def find_equip(eid):
    return equip_store().get(str(int(eid or 0)))






def push_total_consume(session):
    """消耗进度变化后推 5316（TotalConsumeActivityData 全量）。

    客户端原 ackUpdateTotalConsume 是空实现（只 print）；override
    total_consume_push.lua 装了真 handler → setTotalConsumeData + 面板 update()。
    不推的话：面板开着时消耗涨了，其它档本地 status 仍是旧的 → "点不动"。"""
    try:
        from .activities import grade_pay
        grade_pay.sync_dollar_consume()
        snap = grade_pay.total_consume_snapshot()
        if snap is not None:
            session.send(5316, snap.SerializeToString())
    except Exception as _e:
        session.log("push_total_consume 失败: %s" % _e)

def spend_diamond(amount, reason=""):
    """扣元宝（唯一入口）：所有元宝消耗必须走这里 → 自动计入累计消耗活动。

    ★ 累计消耗活动（total_consume）= 记录**元宝实时消耗**，每轮清零
    （2026-10-23 用户明确规则；此前误按金币消费实现）。"""
    p = PLAYER_DATA.setdefault("player", {})
    amt = int(amount or 0)
    if amt <= 0:
        return True
    if int(p.get("diamond") or 0) < amt:
        return False
    p["diamond"] = int(p.get("diamond") or 0) - amt
    # 计数不在这里做——累计消耗活动用「余额差值法」（grade_pay.sync_dollar_consume）
    # 统一统计，防止逐点埋桩遗漏或双重计数（2026-10-23 用户方案）。
    return True


def add_dollar_consume(amount):
    """累计消耗活动计数（元宝实时消耗累计）。"""
    try:
        from .activities import grade_pay
        grade_pay.on_dollar_consume(int(amount or 0))
    except Exception:
        pass

# RMB → 元宝：1 元 = 10 元宝（ShopConfig 充值档 648 元 = 6480 元宝）
DOLLAR_PER_YUAN = 10


def apply_vip_by_pay():
    """按 pay_dollar 重算 VIP 等级（联动 VIPSet.Price 门槛，只升不降）。

    ★ pay_dollar 的单位是**元宝**：VIPSetConfig.Price 与客户端文案
      （“再充值%s元宝即可成为VIP%s”）都以元宝计；1 元 = 10 元宝。
    """
    from . import game_config
    p = PLAYER_DATA.setdefault("player", {})
    pd = int(p.get("pay_dollar") or 0)
    vip = int(p.get("vip") or 0)
    top = game_config.get_vip_max_lv()
    new_vip = vip
    while new_vip < top and pd >= game_config.get_vip_price(new_vip + 1):
        new_vip += 1
    if new_vip != vip:
        p["vip"] = new_vip
        return new_vip
    return None


def migrate_pay_dollar_unit():
    """旧存档 pay_dollar 以人民币累计（元）→ 统一改为元宝（×10）。

    VIPSetConfig.Price / 客户端 VIP 进度文案都是元宝，旧值直接比较会让
    门槛显示成“再充648元宝”而实际是 648 元（2026-10-27 用户报障）。
    迁移只做一次（pay_dollar_unit=10 标记），并同步补 pay_money（人民币）。
    """
    p = PLAYER_DATA.setdefault("player", {})
    if int(p.get("pay_dollar_unit") or 0) == DOLLAR_PER_YUAN:
        return False
    old = int(p.get("pay_dollar") or 0)
    p["pay_dollar"] = old * DOLLAR_PER_YUAN
    if not p.get("pay_money"):
        p["pay_money"] = old
    p["pay_dollar_unit"] = DOLLAR_PER_YUAN
    apply_vip_by_pay()
    return True


def do_buy_prop(item_id, count=1):
    """购买 ShopConfig 项（BuyProp=5210）。

    · Group=4 充值档（Currency=-1 真钱档）：加钻石 + pay_dollar += Price×count
      + VIP 联动 + 首充翻倍（FirstAwards，一次性）+ 单笔/累计充值活动推进。
      支付本身由客户端模拟成功（override 把 gSDK.tryPay 重定向到本协议）。
    · Group=3 月卡/周卡（Currency=-1 真钱档）：与充值档同口径计入 pay_dollar /
      VIP / 充值活动，发 Awards，并开/续有效期（BuyPropRecord 走 590/591）。
    · 其他组（游戏币购买）：按 Currency 扣货币后发 Awards。
    返回 (ok, reason, awards, extra)——extra 供上层决定额外推送。
    """
    from . import game_config
    from . import builders_pb
    item = game_config.get_shop_item(item_id)
    if item is None:
        return False, "商品不存在", [], {}
    p = PLAYER_DATA.setdefault("player", {})
    awards = list(item["awards"])
    extra = {}
    if item["group"] == 4:
        # ---- 充值档 ----
        if not p.get("first_pay_done"):
            awards += list(item["first_awards"])   # 首充翻倍
            p["first_pay_done"] = 1
            extra["first_pay"] = True
        total_price = int(item["price"]) * int(count)
        # pay_dollar 记元宝（VIP 门槛单位），pay_money 记人民币（充值任务显示）
        p["pay_dollar"] = int(p.get("pay_dollar") or 0) + total_price * DOLLAR_PER_YUAN
        p["pay_money"] = int(p.get("pay_money") or 0) + total_price
        p.setdefault("pay_times", 0)
        p["pay_times"] = int(p.get("pay_times") or 0) + 1
        new_vip = apply_vip_by_pay()
        if new_vip is not None:
            extra["vip_up"] = new_vip
        # ---- 充值类活动推进（M2）----
        from . import activities
        activities.on_recharge(total_price, item["price"])
    elif item["group"] == 3:
        # ---- 月卡 / 周卡（真实货币，同充值档口径）----
        from . import month_card
        total_price = int(item["price"]) * int(count)
        p["pay_dollar"] = int(p.get("pay_dollar") or 0) + total_price * DOLLAR_PER_YUAN
        p["pay_money"] = int(p.get("pay_money") or 0) + total_price
        p.setdefault("pay_times", 0)
        p["pay_times"] = int(p.get("pay_times") or 0) + 1
        new_vip = apply_vip_by_pay()
        if new_vip is not None:
            extra["vip_up"] = new_vip
        from . import activities
        activities.on_recharge(total_price, item["price"])
        # 有效期：首次购买新建记录（590），有效期内的续费在剩余天数上叠加（591）
        _rec, _is_new = month_card.buy(item, count)
        extra["buy_prop_record"] = _rec
        extra["buy_prop_new"] = _is_new
    else:
        # ---- 游戏币购买：扣货币 ----
        cur = int(item["currency"])
        price = int(item["price"]) * int(count)
        if cur == 2:      # 钻石
            if int(p.get("diamond") or 0) < price:
                return False, "元宝不足", [], {}
            if not spend_diamond(price, "buy_prop"):
                return False, "元宝不足", [], {}
        elif cur == 1:    # 金币
            if int(p.get("coin") or 0) < price:
                return False, "金币不足", [], {}
            p["coin"] = int(p.get("coin") or 0) - price
        # cur=-1 的其他档按免费处理（扩展位）
    names = _grant_sign_awards(awards)
    return True, "购买成功：" + "、".join(names), awards, extra




def build_award_result_proxy(tag, awards):
    """活动/任务领奖的奖励弹窗帧（301 AwardResult，AwardMessageManager 正路）。"""
    from . import builders_pb
    return builders_pb.build_award_result(tag, _awards_to_specs(awards))


def _grade_status_body(name, grade_key, target):
    """GradStatusData 单档回包（领奖后该档的新状态）。"""
    from ..proto import ps_protobuf_pb2 as _pb
    from .activities import grade_pay
    from .activities import common as _ac
    c = _ac.activity_cfg(name) or {"grades": []}
    hit = next((g for g in c["grades"] if int(g[grade_key]) == target), None)
    m = _pb.GradStatusData()
    m.Grade = target
    m.GradeConsume = target
    st = grade_pay._st(name)
    claimed = set(st.get("claimed") or [])
    m.status = 2 if target in claimed else _ac.grade_status(
        grade_pay._cur_value(name), target, False)
    if hit:
        for a in hit.get("awards") or []:
            spec = m.awards.add()
            _fill_spec_proxy(spec, a)
    return m.SerializeToString()


def _fill_spec_proxy(spec, a):
    if a.get("kind") == "property":
        spec.type = 0
        spec.name = a.get("prop") or ""
        spec.value = int(a.get("value") or 0)
    else:
        spec.type = int(a.get("type") or 0)
        # ★ name 是 required 字段——道具/装备类也必须赋值（空串），
        #   否则 GradStatusData.SerializeToString 抛 EncodeError:
        #   "missing required fields: awards[0].name"（2026-10-23 实测）
        spec.name = ""
        spec.value = int(a.get("count") or 0)

def do_change_hero_position(hero_id, position):
    """上阵/换位（2026-10-22 修"位置不交互"bug）：返回被挤占的武将 dict 或 None。

    · A(已上阵) 调整到 B 占的位置 → **交换**（B 去 A 的原位置）——此前直接把
      B 置 0（未上阵）→ 界面"被顶掉的武将消失但还上阵"，且只回 A 的变更 →
      客户端 B 的本地状态不刷新。
    · A 原本未上阵（position 0/-1）无旧位置可换 → B 下阵（置 0）。
    · 目标位为空 → 纯移动。
    """
    old_pos = 0
    displaced = None
    for h in PLAYER_DATA.get("heroes", {}).values():
        if h["id"] == hero_id:
            old_pos = int(h.get("position") or 0)
        elif int(h.get("position") or 0) == int(position) and h["id"] != hero_id:
            displaced = h
    for h in PLAYER_DATA.get("heroes", {}).values():
        if h["id"] == hero_id:
            h["position"] = position
    if displaced is not None:
        displaced["position"] = old_pos if old_pos > 0 else 0
    return displaced


def hero_equip_list(hero):
    return [find_equip(x) for x in (hero or {}).get("equips", []) if find_equip(x)]


def equip_additions_for(hero):
    """该武将身上所有装备的属性加成 -> [(Type, Value, Mode)]

    ★ 已解锁/洗练的副属性（assist）也计入（2026-10-22 用户要求"副属性真正
    使用起来"）：属性/战力走这里汇总（apply_fate_to_stats），副属性此前被漏掉。
    注意：装备详情面板的"本身属性"显示仍只用 finalAdditions（不含 assist）——
    本处只影响武将总属性/战力。"""
    out = []
    for eq in hero_equip_list(hero):
        out.extend(growth.equip_calc_additions(
            eq.get("type"), eq.get("strengthen", 0), eq.get("grade", 0)))
        for _slot, a in sorted((eq.get("assist") or {}).items(),
                               key=lambda kv: int(kv[0])):
            if a and int(a.get("attr") or 0) > 0:
                out.append((int(a["attr"]), int(a.get("value") or 0), 0))
    return out


def build_addition_data(typ, value, mode=0, index=0, times=0, param=0, param2=0, param3=0, param4=0):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_addition_data(typ, value, mode, index, times, param, param2, param3, param4)


def do_strengthen(equip_id, times=1):
    """装备强化：times = 点击次数（1 或 5）。

    规则（用户 10:49）：
      · 消耗铜币，按 ExpSetConfig[当前强化等级+1] 的部位×品质取值
      · 每次点击有概率暴击，暴击时本次强化次数 = random(1, 倍率上限)
      · 倍率上限 = VIPSetConfig.EquipEnCritCount（VIP8=4 / VIP11=4 / VIP12-15=5）
      · 批量5次 = 5 次点击的**总强化次数一次性累加**，中途不截断
      · 上限（= 玩家等级×2）只用于判断"还能不能强化"，**不截断数值**
        （所以 113 级点强化5次暴击出 20 次 -> 直接到 133 级）
    返回 (ok, reason, eq)
    """
    eq = find_equip(equip_id)
    if not eq:
        return False, "装备不存在", None
    etype = eq.get("type")
    player_lv = int(PLAYER_DATA.get("player", {}).get("lv") or 60)
    vip_lv = int(PLAYER_DATA.get("player", {}).get("vip") or 0)
    max_lv = growth.equip_max_strengthen_lv(player_lv)
    cur = int(eq.get("strengthen") or 0)
    if cur >= max_lv:
        return False, "已达强化上限（%d 级，玩家%d级×2）" % (max_lv, player_lv), eq

    once = growth.equip_strength_cost(etype, cur, False)
    total_cost = once * int(times)
    coin = int(PLAYER_DATA.get("player", {}).get("coin") or 0)
    if coin < total_cost:
        return False, "铜币不足（需 %d，有 %d）" % (total_cost, coin), eq

    crit_rate = growth.equip_crit_rate(vip_lv)
    crit_max = growth.equip_crit_max(vip_lv)
    new_lv, total_add, crit_hits = growth.equip_strengthen_apply(
        cur, times, crit_rate, crit_max)

    PLAYER_DATA["player"]["coin"] = coin - total_cost
    eq["strengthen"] = new_lv
    # EnCritCount：本次暴击产生的额外次数（客户端用它显示"暴击强化等级 +N"）
    eq["en_crit"] = max(0, total_add - int(times))
    return True, ("强化 %d 次 -> +%d 级（暴击 %d 次，倍率上限 %d）"
                  % (times, total_add, crit_hits, crit_max)), eq


def do_grade_up_equip(equip_id, mat_ids):
    """装备升阶（6021）：吃装备实例给进度。

    规则（2026-10-22 逐行对齐客户端 EquipShengJiePanel:getShengJieInfo —— 原
    「diff 比例」自定规则与客户端预览公式不一致，预览 +X 实际只到账零头，
    被用户感知为"升阶没生效"）：
      · 材料（对齐 createEquipFilters）：未穿戴、非自身、品质 ≤ 装备品质
      · 每件经验 = ExpSetConfig[材料品质].EquipGradeExp[材料星级]
                   × (1 + (5 - 装备星级) × 0.2)        —— 浮点累加不取整
      · 铜币 = 总经验 × ExpSetConfig[装备品质].EquipUpGradeExpToCoin
      · 每阶 100 经验（GRADE_FULL_EXP_STEP），上限 9 阶
    返回 (ok, reason, eq)。失败路径 eq 恒为当前装备（绝不为 None —— 空EquipData
    缺 required id 序列化会炸掉 dispatch，客户端收不到任何回包）。
    """
    eq = find_equip(equip_id)
    if not eq:
        return False, "装备不存在", None
    etype = eq.get("type")
    sq, ss = growth.equip_quality(etype), growth.equip_star(etype)
    cur_grade = int(eq.get("grade") or 0)
    cur_exp = int(eq.get("grade_exp") or 0)
    if cur_grade >= growth.EQUIP_MAX_GRADE:
        return False, "已满 9 阶，需先升星", eq

    mats = []
    for mid in (mat_ids or []):
        m = find_equip(int(mid or 0))
        if not m:
            return False, "材料不存在(%s)，可能已被消耗" % mid, eq
        if int(m.get("id")) == int(eq.get("id")):
            return False, "不能消耗自己", eq
        if int(m.get("host") or 0) > 0:
            return False, "已穿戴的装备不能作为材料", eq
        mq = growth.equip_quality(m.get("type"))
        if mq > sq:
            return False, "材料品质高于装备品质", eq
        mats.append((mq, growth.equip_star(m.get("type"))))

    # 经验：逐行对齐客户端 getShengJieInfo（浮点累加，不做逐件取整）
    added_exp = 0.0
    for mq, ms in mats:
        base = growth.equip_grade_base_exp(mq, ms)
        added_exp += base * (1 + (5 - ss) * 0.2)
    if added_exp <= 0:
        return False, "材料提供的升阶经验为 0", eq

    # 铜币 = 总经验 × 装备品质行的 EquipUpGradeExpToCoin（客户端 totalCost 同式）
    row_eq = growth._row_at(sq) or {}
    coin_per = growth._num(row_eq.get("EquipUpGradeExpToCoin"))
    total_cost = int(added_exp * coin_per)
    coin = int(PLAYER_DATA.get("player", {}).get("coin") or 0)
    if coin < total_cost:
        return False, "铜币不足（需 %d，有 %d）" % (total_cost, coin), eq

    # 应用：阶内进度 + 新增 → 进位（对齐面板 floor(addedExp/100)）
    total = cur_exp + int(added_exp)
    new_grade = cur_grade + total // 100
    if new_grade >= growth.EQUIP_MAX_GRADE:
        new_grade = growth.EQUIP_MAX_GRADE
        new_exp = 0
    else:
        new_exp = total % 100
    PLAYER_DATA["player"]["coin"] = coin - total_cost
    eq["grade"] = new_grade
    eq["grade_exp"] = new_exp
    # 材料消耗掉
    for mid in (mat_ids or []):
        _drop_equip(int(mid))
    return True, ("升阶 → %d阶 %d/100 经验（吃%d件，+%d经验，耗币%d）"
                  % (new_grade, new_exp, len(mats), int(added_exp), total_cost)), eq


def _drop_equip(eid):
    eid = int(eid or 0)
    eq = find_equip(eid)
    if not eq:
        return
    host = _find_hero(int(eq.get("host") or 0))
    if host:
        host["equips"] = [x for x in host.get("equips", []) if x != eid]
    equip_store().pop(str(eid), None)


def do_star_up_equip(equip_id, mat_ids):
    """装备升星（6023）：吃 2 件同星同品质同名实例 + 铜币，换到下一星级形态。

    规则（用户 10:49 + 12:55）：
      · 前置：当前星级满 9 阶
      · 材料：2 件同星同品质同名（同 Group）装备实例
      · 铜币：EquipStarUpCost[当前星级]（1→2=1万 / 2→3=3万 / 3→4=10万 / 4→5=30万）
      · 升星后：**阶数清零**，**强化等级保留不动**
      · 星级上限 = MaxQuality（蓝3 / 紫4 / 神5）
      · 走 EquipConfig 的 LastType/NextType 链切换形态
    返回 (ok, reason, eq)
    """
    eq = find_equip(equip_id)
    if not eq:
        return False, "装备不存在", None
    etype = eq.get("type")

    # 材料先解析，校验用
    mats = []
    for mid in (mat_ids or []):
        m = find_equip(int(mid or 0))
        if not m:
            return False, "材料不存在(%s)" % mid, None
        if int(m.get("id")) == int(eq.get("id")):
            return False, "不能消耗自己", None
        if int(m.get("host") or 0) > 0:
            return False, "已穿戴的装备不能作为材料", None
        mats.append(m.get("type"))

    ok, why = growth.equip_can_star_up(etype, eq.get("grade"), mats)
    if not ok:
        return False, why, eq

    cost = growth.equip_star_up_cost(growth.equip_star(etype))
    coin = int(PLAYER_DATA.get("player", {}).get("coin") or 0)
    if coin < cost:
        return False, "铜币不足（需 %d，有 %d）" % (cost, coin), eq

    new_type = growth.equip_star_up_apply(etype)
    old_star = growth.equip_star(etype)
    PLAYER_DATA["player"]["coin"] = coin - cost
    eq["type"] = new_type
    eq["grade"] = 0            # 阶数清零
    eq["grade_exp"] = 0        # 阶经验清零
    # strengthen（强化等级）保留不动
    for mid in (mat_ids or []):
        _drop_equip(int(mid))
    return True, ("升星 %d→%d 星（type %s→%s），阶数清零，强化Lv=%s 保留，耗币%d"
                  % (old_star, growth.equip_star(new_type), etype, new_type,
                     eq.get("strengthen"), cost)), eq


def do_reform_equip(equip_id, slot, player_lv):
    """改造/洗练副属性（6025）：消耗铜币+洗练石，随机刷出新属性（暂存待保存）。

    slot: 副属性位 1 或 2（AssistantAddition.IndexID）
    解锁条件：3星开1条、5星开2条（EquipSubAddCount）
    属性池：ProbabilitySetConfig，攻击类只刷攻击/连击/暴击/暴伤，防御类只刷防御/格挡/反击/生命

    消耗口径（2026-10-28 对齐客户端 CostGroup）：
      · 洗练石 = ExpSetConfig[星级].EquipReformStone（固定）
      · 铜币   = ExpSetConfig[星级].EquipReformCoin × (玩家等级 + 已洗练次数 times)
      · 洗练石不足时的「元宝补充」：客户端 CostGroup:requestReform 弹
        「洗练石不足，是否花费 (100+times) 元宝购买洗练石？」，确认后
        **原样重发 6025**（请求里没有任何标记）——服务端必须自己推断：
        石头不够但元宝 ≥ 100+times → 扣这笔元宝代付洗练石（石头不扣），
        照常完成洗练。以前这里直接 5002 拒绝 → 玩家点了确认还是失败。
    返回 (ok, reason, eq)
    """
    eq = find_equip(equip_id)
    if not eq:
        return False, "装备不存在", None
    etype = eq.get("type")
    slot = int(slot or 0)
    cfg = growth.equip_assistant_cfg(etype)
    entry = next((c for c in cfg if c["index"] == slot), None)
    if not entry:
        return False, "该装备没有第 %d 条副属性" % slot, None
    if growth.equip_star(etype) < entry["need_star"]:
        return False, "需 %d 星解锁该副属性" % entry["need_star"], None

    base_coin, cost_stone = growth.equip_reform_cost(growth.equip_star(etype))
    # 已洗练次数：与客户端读到的 wire 值一致（build_equip_data 发 int(times or 1)）
    cur = (eq.get("assist") or {}).get(str(slot)) or {}
    times = int(cur.get("times") or 1)
    cost_coin = int(base_coin) * (int(player_lv or 0) + times)
    inv = PLAYER_DATA.setdefault("items", {})
    stone_id = "20002"          # 改造石（ItemConfig 20002 改造石）
    have_stone = int(inv.get(stone_id, 0))
    coin = int(PLAYER_DATA.get("player", {}).get("coin") or 0)
    diamond = int(PLAYER_DATA.get("player", {}).get("diamond") or 0)

    # ---- 校验全部通过才扣费（先算后扣，抽失败不扣钱）----
    buy_stone_with_diamond = False
    need_diamond = 0
    if have_stone < cost_stone:
        # 洗练石不足 → 元宝补充（客户端弹窗价 = 100 + times，与 needDollar 一致）
        need_diamond = 100 + times
        if diamond < need_diamond:
            return False, ("洗练石不足（需 %d，有 %d）；元宝补充需 %d，现有 %d"
                           % (cost_stone, have_stone, need_diamond, diamond)), eq
        buy_stone_with_diamond = True
    if coin < cost_coin:
        return False, "铜币不足（需 %d，有 %d）" % (cost_coin, coin), eq

    attr, value, lo, hi = growth.equip_reform_roll(entry["kind"], player_lv)
    if attr <= 0:
        return False, ("副属性池为空（类型 %s / 玩家 Lv%s）"
                       % (entry["kind"], player_lv)), eq

    # ---- 扣费 ----
    PLAYER_DATA["player"]["coin"] = coin - cost_coin
    if buy_stone_with_diamond:
        # 元宝代付洗练石：玩家手里已有的石头不动（付的是"这次洗练的石头钱"）
        spend_diamond(need_diamond, "reform_stone")
    elif cost_stone > 0:
        inv[stone_id] = have_stone - cost_stone
    # 暂存到 pending（未保存），客户端走 6026 保存 / 6027 放弃
    # min/max 必须存：客户端 AdditionBtn:getColor 用它做颜色分档（蓝/紫/金）
    pend = eq.setdefault("pending_assist", {})
    pend[str(slot)] = {"attr": attr, "value": value,
                       "kind": entry["kind"], "times": times + 1,
                       "cost_coin": cost_coin, "cost_stone": cost_stone,
                       "min": lo, "max": hi}
    if buy_stone_with_diamond:
        return True, ("洗练第%d条(%s类) → 属性%d=%d（洗练石不足，耗元宝%d 代付 + 币%d）"
                      % (slot, "攻" if entry["kind"] == 1 else "防",
                         attr, value, need_diamond, cost_coin)), eq
    return True, ("洗练第%d条(%s类) → 属性%d=%d（耗币%d 洗练石%d）"
                  % (slot, "攻" if entry["kind"] == 1 else "防",
                     attr, value, cost_coin, cost_stone)), eq


def do_save_reform(equip_id, slot):
    """保存洗练结果（6026）：把 pending 写入正式副属性。

    保存条件（对齐客户端 CostGroup 的按钮门禁，服务端同样强制）：
      · 刷出属性与当前【不同】→ 可保存
      · 刷出属性与当前【相同】：新值 >  当前 → 只允许保存
                                新值 <  当前 → 只允许放弃（保存被拒）
                                新值 == 当前 → 允许保存
    """
    eq = find_equip(equip_id)
    if not eq:
        return False, "装备不存在", None
    pend = (eq.get("pending_assist") or {}).get(str(slot))
    if not pend:
        return False, "没有待保存的洗练结果", None
    cur = (eq.get("assist") or {}).get(str(slot))
    if cur and int(cur.get("attr") or 0) == int(pend.get("attr") or 0):
        if int(pend.get("value") or 0) < int(cur.get("value") or 0):
            return False, ("相同属性且数值低于当前（%d < %d），只能放弃"
                           % (int(pend.get("value") or 0), int(cur.get("value") or 0))), eq
    assists = eq.setdefault("assist", {})
    assists[str(slot)] = dict(pend)
    eq.get("pending_assist", {}).pop(str(slot), None)
    return True, "已保存第 %d 条副属性" % int(slot), eq


def do_giveup_reform(equip_id, slot):
    """放弃洗练结果（6027）"""
    eq = find_equip(equip_id)
    if not eq:
        return False, "装备不存在", None
    (eq.get("pending_assist") or {}).pop(str(slot), None)
    return True, "已放弃", eq


# ============================== 签到（10003 签到 / 10004 补签）=============================
# 对齐客户端 SignInPanel/SignManager/CharacterData 字段：
#   signTimes(11)=累计签到 | missSignTimes(12)=当月漏签 | todaySignTimes(13)=今日次数(0/1/2)
#   bugSignTimes(21)=当月补签次数 | signId(22)=当月进度(已领天数)
# 规则（用户 2026-10-22）：上线一天签一天、奖励按序领取；漏签可用元宝补签（补签=领
# 下一天奖励）；每月刷新进度；部分奖励按 VIP 双倍领取。
OP_SIGN = 10003
OP_BUY_SIGN = 10004


def _sign_today():
    import datetime
    t = datetime.date.today()
    return t.year, t.month, t.day, "%04d%02d%02d" % (t.year, t.month, t.day)


def _sign_state():
    """签到状态（含月度刷新 + 今日次数重置 + 动态漏签数）。"""
    y, mo, d, ymd = _sign_today()
    ym = "%04d%02d" % (y, mo)
    st = PLAYER_DATA.setdefault("sign", {})
    if st.get("month") != ym:
        # ★ 每月刷新进度：进度/漏签/补签次数清零（累计签到次数保留）
        st["month"] = ym
        st["sign_id"] = 0
        st["today_sign_times"] = 0
        st["bug_sign_times"] = 0
        st["last_day"] = ""
    if st.get("last_day") != ymd:
        st["today_sign_times"] = 0
    sign_id = int(st.get("sign_id") or 0)
    today_signed = int(st.get("today_sign_times") or 0) > 0
    # ★ 可补签数（2026-10-22 修"少算一天"）：本月到今天共 d 天可领（1..today），
    #   已领 sign_id 天；今天未签时还留 1 次免费签到 → 其余全部走补签。
    #   此前用 (d-1)-sign_id 少算今天那一格：1签+20补=21 就封顶，今天格子领不到；
    #   正确是 1签+21补=22 恰好覆盖 1..22 号。
    miss = max(0, d - sign_id - (0 if today_signed else 1))
    return {
        # ★ 2026-11-01 修正：客户端 SignInPanel.buildSignDataList 用 signTimes 判定
        #   "本月已签天数"（index <= signTimes → 显示已领取），文案也是"本月已签到 N 次"。
        #   原先这里下发的是**跨月累计** st["sign_times"]（每月重置时特意保留），
        #   结果第二个月起 1~30 号格子全部显示"已领取"（跨月不刷新，用户报障）。
        #   累计值仍存在 st["sign_times"] 里供内部统计，下发一律用当月进度 sign_id。
        "sign_times": sign_id,
        "miss_sign_times": miss,
        "today_sign_times": int(st.get("today_sign_times") or 0),
        "bug_sign_times": int(st.get("bug_sign_times") or 0),
        "sign_id": sign_id,
    }


def pay_display():
    """充值字段（CharacterData 25/26/31/40）。

    ★ VIP 进度条数学（PayPanel）：cur = payDollar - VIP[当前档].Price，
    进度 = cur / (VIP[下一档].Price - 当前档.Price)，再 setScaleX——
    payDollar 缺失(-1) 会让 cur 为负 → scaleX 负数 → 进度条**水平反向**（2026-10-22）。
    无存档值时按当前 VIP 档门槛初始化（进度 0%，语义正确）。

    ★ payDollar 单位是元宝（VIPSet.Price/客户端文案同单位）；payMoney 是
      人民币累计（DailyWelfare 的充值任务按人民币比较），两者不能混用。
    """
    from . import game_config
    p = PLAYER_DATA.get("player", {})
    vip = int(p.get("vip") or 0)
    pd = p.get("pay_dollar")
    if pd is None:
        pd = game_config.get_vip_price(vip)   # 至少到本档门槛，保证 cur >= 0
        p["pay_dollar"] = pd
    pm = p.get("pay_money")
    if pm is None:
        pm = int(pd) // DOLLAR_PER_YUAN
    return {"payDollar": int(pd), "payTimes": int(p.get("pay_times") or 1),
            "payMoney": int(pm), "payMoneyOfDay": 0}


def sign_display():
    """给 character_msg 下发的 5 个签到字段。"""
    return _sign_state()


def _sign_awards_today():
    from . import game_config
    y, mo, d, _ = _sign_today()
    return game_config.get_month_sign_awards(y, mo)


def _grant_sign_awards(awards):
    """发放签到/收集奖励。两类（对齐 SignAwardConfig / CollectionAwardConfig）：
      ObjectAward{Type,Count}      → 道具 items（**非道具要按类型路由**，见下）
      PropertyAward{PropertyName,Value} → 属性（Dollar=元宝 / Coin=铜币 / Vigour 等）
    返回描述列表。

    ★ 2026-10-22 修：ObjectAward 的 Type 不一定是道具——签到/收集配置里可能是
      装备(33xxx)、武将或头像框。以前一律塞进 items，导致客户端 BagManager
      `addItem` → `getItemTypeCache` 取不到 ItemConfig，登录直接 LUA ERROR
      （实测 33903 方天画戟被当道具入库）。现在按配置表分流。
    """
    from . import game_config
    p = PLAYER_DATA.setdefault("player", {})
    prop_map = {"Dollar": "diamond", "Coin": "coin", "Vigour": "vigour",
                "EP": "ep", "HeroCoin": "hero_coin"}
    heads, frames = game_config.honor_ids()
    honor_ids = set(heads) | set(frames)
    desc = []
    for a in awards:
        if a.get("kind") == "property":
            key = prop_map.get(a["prop"])
            if key:
                p[key] = int(p.get(key) or 0) + int(a["value"])
                desc.append("%s×%d" % (a["prop"], int(a["value"])))
            else:
                desc.append("%s×%d(未识别)" % (a["prop"], int(a["value"])))
            continue
        t, c = int(a["type"]), int(a["count"])
        if game_config.item_exists(t):
            # 卡类（武将卡/定缘卡）走实例模型：add_item 逐张建实例并广播 401
            add_item(t, c)
            desc.append("%s×%d" % (game_config.get_item_name(t), c))
            continue
        if t in honor_ids:
            desc.append("头像/头像框×1")
            continue
        try:
            if growth.is_equip_ownable(t):
                from . import builders_pb
                for _ in range(max(1, c)):
                    eid = _create_equip_instance(t)
                    broadcast(403, builders_pb.build_equip_data(find_equip(eid)))
                desc.append("%s×%d" % (game_config.get_equip_name(t), c))
                continue
            if growth.find_hero_row(t):
                from . import builders_pb
                for _ in range(max(1, c)):
                    hid = _create_hero_instance(t)
                    broadcast(404, build_hero_data(hid, t, 1, 0))
                    _HERO_404_SEEN.add(int(hid))   # 防后续 _push_new_heroes 重复推（见该函数注释）
                desc.append("%s×%d" % (game_config.get_hero_name(t), c))
                continue
        except Exception as e:
            flog("[grant] 非道具奖励发放失败 type=%s: %s" % (t, e))
        desc.append("未知奖励(%s)×%d" % (t, c))
    return desc


def _award_spec_bytes(type_id, count, extra=False, name=""):
    """单件 AwardSpecData 字节（LevelResult.props 用）。

    `extra=True` 让客户端 AwardContainer 归到"额外奖励"分组（武将/装备类）。
    name 是 required 字段，必须赋值（空串也要写）。
    """
    from ..proto import ps_protobuf_pb2 as _pb
    m = _pb.AwardSpecData()
    m.type = int(type_id or 0)
    m.name = name or ""
    m.value = max(1, int(count or 1))
    if extra:
        m.extra = True
    return m.SerializeToString()


def _awards_to_specs(awards):
    """奖励列表 -> AwardSpecData 元组：属性 (0, value, prop) / 道具 (type, count)。"""
    specs = []
    for a in awards:
        if a.get("kind") == "property":
            specs.append((0, int(a.get("value") or 0), str(a.get("prop") or "")))
        else:
            specs.append((int(a.get("type") or 0), int(a.get("count") or 0)))
    return specs


def _double_awards(awards):
    """VIP 双倍：奖励数量 ×2（弹窗显示也是 ×2）。"""
    out = []
    for a in awards:
        b = dict(a)
        if b.get("kind") == "property":
            b["value"] = int(b.get("value") or 0) * 2
        else:
            b["count"] = int(b.get("count") or 0) * 2
        out.append(b)
    return out


def do_sign():
    """签到（10003）：按序推进一天；today=1 且当天奖励 VIPLv 达标 → 可双倍再领一次。

    返回 (ok, reason, awards)。"""
    st = PLAYER_DATA.setdefault("sign", {})
    state = _sign_state()
    awards = _sign_awards_today()
    idx = state["sign_id"]
    _, _, day, _ = _sign_today()
    if idx >= day:
        return False, "已领到今天，明天再来", []
    if idx >= len(awards):
        return False, "本月签到已领完", []
    vip = int(PLAYER_DATA.get("player", {}).get("vip") or 0)
    _, _, _, ymd = _sign_today()
    if state["today_sign_times"] == 0:
        entry = awards[idx]
        st["sign_id"] = idx + 1
        st["sign_times"] = int(st.get("sign_times") or 0) + 1
        st["last_day"] = ymd
        # ★ VIP 双倍（用户 2026-10-22）：奖励带 VIPLv 且 VIP 达标 → **本次直接发
        #   双倍数量**（弹窗 value×2、一锤子领完），today 直接置 2 不再留二次入口
        if entry["vip_lv"] > 0 and vip >= entry["vip_lv"] and entry["extra_times"] >= 1:
            st["today_sign_times"] = 2
            return True, "签到成功（VIP双倍）", _double_awards(entry["awards"])
        st["today_sign_times"] = 1
        return True, "签到成功", entry["awards"]
    if state["today_sign_times"] == 1:
        # 二次领取兜底：首签时 VIP 不足（today=1），充 VIP 后再点补发另一倍（总 2×）
        cur = awards[idx - 1] if 0 < idx <= len(awards) else None
        if cur and cur["vip_lv"] > 0 and vip >= cur["vip_lv"] and cur["extra_times"] >= 1:
            st["today_sign_times"] = 2
            st["last_day"] = ymd
            return True, "VIP 双倍补领", cur["awards"]
        return False, "今天已签到，无双倍可领", []
    return False, "今天已签到", []


def do_buy_sign():
    """补签（10004）：VIP≥5、有漏签；耗元宝 min(2^(补签次数+1)×10, 160)；领下一天奖励。"""
    st = PLAYER_DATA.setdefault("sign", {})
    state = _sign_state()
    if state["miss_sign_times"] <= 0:
        return False, "没有漏签天数，不能补签", []
    vip = int(PLAYER_DATA.get("player", {}).get("vip") or 0)
    if vip < 5:
        return False, "补签需要 VIP5", []
    cost = min(2 ** (int(st.get("bug_sign_times") or 0) + 1) * 10, 160)
    diamond = int(PLAYER_DATA.get("player", {}).get("diamond") or 0)
    if diamond < cost:
        return False, "元宝不足（需 %d，有 %d）" % (cost, diamond), []
    awards = _sign_awards_today()
    idx = state["sign_id"]
    _, _, day, _ = _sign_today()
    if idx >= day:
        return False, "已领到今天，无天可补", []
    if idx >= len(awards):
        return False, "本月签到已领完", []
    entry = awards[idx]
    if not spend_diamond(cost, "buy_sign"):
        return False, "元宝不足", []
    st["sign_id"] = idx + 1
    st["bug_sign_times"] = int(st.get("bug_sign_times") or 0) + 1
    st["sign_times"] = int(st.get("sign_times") or 0) + 1
    got = entry["awards"]
    tag = ""
    if entry["vip_lv"] > 0 and vip >= entry["vip_lv"] and entry["extra_times"] >= 1:
        got = _double_awards(got)   # 补签奖励同样享受 VIP 双倍
        tag = "（VIP双倍）"
    return True, "补签成功%s（耗元宝 %d）" % (tag, cost), got


def do_decompose_equips(equip_ids):
    """分解装备（20210）：只接受未穿戴的，返还铜币+洗练石（80%）"""
    coin_total, stone_total = 0, 0
    for eid in (equip_ids or []):
        eq = find_equip(int(eid))
        if not eq:
            return False, "装备不存在(%s)" % eid
        if int(eq.get("host") or 0) > 0:
            return False, "已穿戴的装备不能分解", None
        assists = [{"times": v.get("times"), "cost_coin": v.get("cost_coin"),
                    "cost_stone": v.get("cost_stone")}
                   for v in (eq.get("assist") or {}).values()]
        c, s = growth.equip_decompose_refund(
            eq.get("type"), eq.get("strengthen"), eq.get("grade"),
            eq.get("grade_exp"), assists)
        coin_total += c
        stone_total += s
        _drop_equip(int(eid))
    inv = PLAYER_DATA.setdefault("items", {})
    PLAYER_DATA["player"]["coin"] = int(PLAYER_DATA["player"].get("coin") or 0) + coin_total
    if stone_total > 0:
        inv["20002"] = int(inv.get("20002", 0)) + stone_total
    return True, ("分解 %d 件 → 返还铜币 %d、洗练石 %d"
                  % (len(equip_ids or []), coin_total, stone_total)), None


def do_synthesize(frag_id, player_lv=60):
    """碎片合成（5020）：一键把该碎片能合的全部合掉。

    规则（用户 14:15）：floor(拥有 / 需求) 件，余下碎片保留
      110/30 → 3 件（余 20）    80/80 → 1 件    59/50 → 1 件（余 9）
    每件收一次手续费（SyntheticConfig.Coin）。
    返回 (ok, reason, new_equip_ids)
    """
    e = growth.synth_entry(frag_id)
    if not e:
        return False, "该道具不是装备碎片", []
    inv = PLAYER_DATA.setdefault("items", {})
    frag_id = int(frag_id)
    have = int(inv.get(str(frag_id), 0))
    n, cost_frag, cost_coin = growth.synth_result(frag_id, have)
    if n <= 0:
        return False, "碎片不足（需 %d，有 %d）" % (e["need"], have), []
    coin = int(PLAYER_DATA.get("player", {}).get("coin") or 0)
    if coin < cost_coin:
        return False, "铜币不足（需 %d，有 %d）" % (cost_coin, coin), []

    PLAYER_DATA["player"]["coin"] = coin - cost_coin
    inv[str(frag_id)] = have - cost_frag
    eq_store = equip_store()
    top = max([int(k) for k in eq_store] or [0])
    new_ids = []
    for i in range(n):
        top += 1
        new_ids.append(top)
        eq_store[str(top)] = {"id": top, "type": e["product"], "host": 0,
                              "strengthen": 0, "grade": 0, "grade_exp": 0}
        _record_equip_collect(e["product"], top)
    return True, ("合成 %d 件「%s」（消耗碎片 %d，余 %d，耗币 %d）"
                  % (n, e.get("name") or e["product"], cost_frag,
                     have - cost_frag, cost_coin)), new_ids


UNLOCK_ASSIST_COST = 300   # 解锁副属性消耗元宝（BagManager:unlockEquipAssistantAddition {cost=300}）


def do_unlock_assistant(equip_id, slot, player_lv=60):
    """解锁副属性（6024 UnlockAssistantAddition）：消耗 300 元宝，随机开出一条副属性。

    前置：该装备的 AssistantAddition 里存在该 IndexID，且当前星级 >= Condition
          （3★ 解锁第 1 条 / 5★ 解锁第 2 条）
    返回 (ok, reason, eq)
    """
    eq = find_equip(equip_id)
    if not eq:
        return False, "装备不存在", None
    etype = eq.get("type")
    slot = int(slot or 0)
    cfg = growth.equip_assistant_cfg(etype)
    entry = next((c for c in cfg if c["index"] == slot), None)
    if not entry:
        return False, "该装备没有第 %d 条副属性" % slot, None
    star = growth.equip_star(etype)
    if star < entry["need_star"]:
        return False, "需 %d 星解锁该副属性（当前 %d 星）" % (entry["need_star"], star), None
    if (eq.get("assist") or {}).get(str(slot)):
        return False, "该副属性已解锁", None

    diamond = int(PLAYER_DATA.get("player", {}).get("diamond") or 0)
    if diamond < UNLOCK_ASSIST_COST:
        return False, "元宝不足（需 %d，有 %d）" % (UNLOCK_ASSIST_COST, diamond), eq

    attr, value, lo, hi = growth.equip_reform_roll(entry["kind"], player_lv)
    if attr <= 0:
        # 池空只可能是「该 kind 的属性池没配/玩家等级未解锁任何档」——
        # 以前一律报"玩家等级不够"，误导排查（2026-10-28 七星龙渊剑 Type=4 事件）
        return False, ("副属性池为空（类型 %s / 玩家 Lv%s，可能配置缺失或等级未达 OpenLv）"
                       % (entry["kind"], player_lv)), eq
    cost_coin, cost_stone = growth.equip_reform_cost(star)
    if not spend_diamond(UNLOCK_ASSIST_COST, "reform_assist"):
        return False, "元宝不足（需 %d，有 %d）" % (UNLOCK_ASSIST_COST, diamond), eq
    # times 必须 >=1：客户端 AssistantAddition:updateState 以 times>0 判定
    # 「已解锁（state 2，显示洗练按钮）」；解锁即第一次掷属性，计 1 次。
    eq.setdefault("assist", {})[str(slot)] = {
        "attr": attr, "value": value, "kind": entry["kind"], "times": 1,
        "cost_coin": cost_coin, "cost_stone": cost_stone,
        "min": lo, "max": hi}
    return True, ("解锁第%d条副属性 → 属性%d=%d（耗元宝%d）"
                  % (slot, attr, value, UNLOCK_ASSIST_COST)), eq


def build_equip_data(eq):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_equip_data(eq)


def do_wear_equip(equip_id, hero_id):
    """穿戴：把装备挂到武将身上。返回 (ok, reason)"""
    eq = find_equip(equip_id)
    if not eq:
        return False, "装备不存在"
    hero = _find_hero(hero_id)
    if not hero:
        return False, "武将不存在"
    # 同部位只能穿一件：先卸下同部位的旧装备
    part = growth.equip_part(eq.get("type"))
    worn = [find_equip(x) for x in hero.get("equips", [])]
    for old in worn:
        if old and growth.equip_part(old.get("type")) == part:
            old["host"] = 0
            hero["equips"] = [x for x in hero.get("equips", []) if x != old.get("id")]
    # 从原穿戴者身上摘下
    if int(eq.get("host") or 0) > 0:
        prev = _find_hero(eq["host"])
        if prev:
            prev["equips"] = [x for x in prev.get("equips", []) if x != eq.get("id")]
    eq["host"] = int(hero["id"])
    hero.setdefault("equips", []).append(eq["id"])
    return True, "穿戴成功"


def do_down_equip(equip_id):
    """卸下"""
    eq = find_equip(equip_id)
    if not eq:
        return False, "装备不存在"
    host_id = int(eq.get("host") or 0)
    if host_id <= 0:
        return False, "装备未穿戴"
    hero = _find_hero(host_id)
    if hero:
        hero["equips"] = [x for x in hero.get("equips", []) if x != eq.get("id")]
    eq["host"] = 0
    return True, "卸下成功"


def _activated_fate_ids_for_group(group):
    """某武将名（Group）下已「永久激活」的缘分 id 列表。

    ★ 2026-10-27 用户拍板生效规则：
      · 永久激活（进度全满）的缘分按**武将名绑定**——同名所有实例（任意品质/
        星级）默认激活并使用强化值；没有实例就挂着，将来获得同名实例自动生效；
        不同名武将完全无关。
      · Owner（FateConfig）= 拥有者武将 Group；同名不同品质同 Group。
    """
    hit = _req_cache_get("activated_fates_g%s" % group)
    if hit is not None:
        return hit
    out = []
    for fid in list(fate_store().keys()):
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            continue
        row = growth.fate_row(fid_i) or {}
        if int(growth._num(row.get("Owner"))) != int(group):
            continue
        # on_field_groups/equipped 传空：state=2 的判定只看进度 complete
        state, _up = growth.fate_active_state(fid_i, fate_progress(fid_i), (), None)
        if state == 2:
            out.append(fid_i)
    return _req_cache_put("activated_fates_g%s" % group, out)


def _equip_fate_index():
    """所有装备缘（kind=1）索引：{owner_group: [(fate_id, 目标装备Group)]}。

    ★ 吕布(10856)等武将的 HeroConfig.CanRelation 为空，hero_fate_ids 查不到
      自己的装备缘（如 1343 飞将紫金冠 +22% 攻击）→ 穿上专属装也不生效
      （2026-10-27 用户报障）。装备缘统一按 Owner 扫描兜底。
    """
    hit = _req_cache_get("equip_fate_idx")
    if hit is not None:
        return hit
    growth._load_fate()
    idx = {}
    for fid, row in (growth._fate_cfg or {}).items():
        try:
            if int(growth._num(row.get("Group"))) != 1:
                continue
            owner = int(growth._num(row.get("Owner")))
            targets = growth.fate_targets(fid)
            if owner > 0 and targets:
                idx.setdefault(owner, []).append((fid, targets[0]))
        except Exception:
            continue
    return _req_cache_put("equip_fate_idx", idx)


def fate_bonuses_for(hero):
    """该武将实例当前生效的缘分加成 [(Type, Value, Mode)]。

    ★ 2026-10-27 用户拍板生效规则：
      · 永久激活（进度全满）→ **同名所有实例**生效（用强化值），不上阵也生效，
        不同名武将无关；
      · 装备缘 → 只对**穿着该装备的实例**生效（equipped 判定，现状）；
      · 上阵临时激活（未满但条件武将在场）→ 只对**当前上阵的该实例**生效，
        同名仓库实例不吃；与永久激活并存时永久优先（强化值替换普通值）。
    """
    if not hero:
        return []
    tid = hero.get("type")
    groups = on_field_groups()
    equipped = hero_equipped_ids(hero)
    on_field = int(hero.get("position") or 0) > 0
    out = []
    own_done = set()
    for fid in growth.hero_fate_ids(tid):
        state, upgraded = growth.fate_active_state(
            fid, fate_progress(fid), groups, equipped)
        if state == 2:
            own_done.add(fid)
            out.extend(growth.fate_additions(fid, True))    # 永久激活：强化值
        elif state == 1:
            # 临时生效：装备缘穿着即生效（不限上阵）；人与人缘只对上阵实例
            if growth.fate_kind(fid) == 1 or on_field:
                out.extend(growth.fate_additions(fid, False))
                # ★ 2026-10-28 去重修复：这里必须登记 own_done。
                #   否则下面的「装备缘兜底（按 Owner 扫描）」会再扫到同一个 fid
                #   并重复叠加 —— 赵云 6★ 戴神龙胆亮银枪：缘分 1139（装备缘 +35%攻）
                #   在 CanRelation 里，首段生效后兜底段又加一次 → 百分比 70% 而非 35%，
                #   面板攻击 12162 而正确值 9658（用户报障）。
                own_done.add(fid)
    # 同名永久激活兜底（实例 type 的 CanRelation 未覆盖/新获得实例时同样生效）
    for fid in _activated_fate_ids_for_group(growth.hero_group(tid)):
        if fid not in own_done:
            out.extend(growth.fate_additions(fid, True))
    # 装备缘兜底：按 Owner 扫描（CanRelation 空的武将也能吃到专属装缘分）
    for fid, tgt in _equip_fate_index().get(growth.hero_group(tid)) or []:
        if fid in own_done:
            continue
        if equipped and tgt in equipped:
            out.extend(growth.fate_additions(fid, False))
            own_done.add(fid)
    return out


def apply_fate_to_stats(stats, hero, extra_bonuses=None):
    """把「缘分 + 装备 + 编队级加成(军旗)」一次性叠到属性上，并重算战力。

    ★ 必须一次算完：`apply_addition_bonus` 的百分比以**调用前的值**为基数，
    分成两次调用就会变成「第二次乘在第一次结果上」的复利（旧实现的问题）。
    加成顺序不影响结果（同类百分比相加，固定值不参与百分比放大），
    公式见 growth.apply_addition_bonus 文档。

    extra_bonuses：编队级加成（军旗词条 + 套装），由调用方决定是否传。
    """
    if not stats:
        return stats
    bonuses = fate_bonuses_for(hero) + equip_additions_for(hero)
    if extra_bonuses:
        bonuses = bonuses + list(extra_bonuses)
    if not bonuses:
        return stats
    growth.apply_addition_bonus(stats, bonuses, growth.EQUIP_TYPE_ATTR)
    stats["power"] = growth.calc_power(stats)
    return stats


def build_fate_info():
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_fate_info()


# 配缘铜币消耗（按材料品质/星级）——与客户端 FatePanel 的 useCoin 表逐值一致：
#   3★=10万 / 4★=20万 / 5★=40万 / 6★=80万，1~2★ 不消耗
FATE_COIN_COST = {1: 0, 2: 0, 3: 100000, 4: 200000, 5: 400000, 6: 800000}


def fate_coin_cost(star):
    return int(FATE_COIN_COST.get(int(star or 0), 0))


def do_fasten_fate(owner_hero, mat_id):
    """强化缘分：用「替身卡」或「对应武将实例」给某条缘分的某个条件加进度。

    规则（用户 17:40）：
      · 4★材料 +3 / 5★ +10 / 6★ +30（3★ +1，见 FateConfig.ExpSetConfig）
      · **配缘消耗铜币**：按材料品质/星级扣（3★10万 / 4★20万 / 5★40万 / 6★80万）
      · 进度按「武将 Group」全局共享，与实例无关，永久保存
      · 进度达到 TargetExp[该 group] → 该条件 Complete = 1
      · 全部条件 Complete → 缘分永久激活（加成替换为 UpgradeAdditions）
    返回 (ok, reason, touched_fate_ids)
    """
    if not owner_hero:
        return False, "武将不存在", [], None
    otype = owner_hero.get("type")
    # 1) 材料是替身卡（道具）还是武将实例
    #    卡类是实例模型：请求里的 mat_id 是唯一实例 id，先解析回卡 type
    mat_type = resolve_item_type(mat_id)
    target_group = None
    progress_add = 0
    consume_item = False
    mat_hero = None
    info = growth.fate_item_info(mat_type)
    if info:
        target_group, star = info
        progress_add = growth.fate_progress_for_star(star)
        consume_item = True
    else:
        mat_hero = _find_hero(mat_id)
        if not mat_hero:
            return False, "材料不存在（%s）" % mat_id, [], None
        target_group = growth.hero_group(mat_hero.get("type"))
        mrow = growth.find_hero_row(mat_hero.get("type"))
        star = int(growth._num(mrow.get("Quality"))) if mrow else 0
        progress_add = growth.fate_progress_for_star(star)
    if progress_add <= 0:
        return False, "材料星级无效（只支持 3~6 星）", [], None

    # 2) 找出该武将里「以 target_group 为条件」的那些缘分
    hit = []
    for fid in growth.hero_fate_ids(otype):
        if growth.fate_kind(fid) == 1:
            continue                                  # 装备缘不靠进度
        for i, g in enumerate(growth.fate_targets(fid)):
            if g == target_group:
                need = growth.fate_target_exp(fid)
                hit.append((fid, i, need[i] if i < len(need) else 0))
    if not hit:
        return False, "该材料与武将的任何缘分都不匹配", [], None

    # 3) 扣铜币 + 扣材料 + 加进度
    p = PLAYER_DATA.setdefault("player", {})
    coin_cost = fate_coin_cost(star)
    if coin_cost > 0 and int(p.get("coin") or 0) < coin_cost:
        return False, "铜币不足（需要 %d）" % coin_cost, [], None
    if consume_item:
        if card_type_of(mat_id):
            # 卡实例：整张消耗（count 恒 1）。已不存在说明客户端与服务端不同步
            if item_cards().pop(str(int(mat_id)), None) is None:
                return False, "该缘分卡已不存在，请重新进入", [], None
        else:
            inv = PLAYER_DATA.setdefault("items", {})
            have = int(inv.get(str(mat_type), 0))
            if have < 1:
                return False, "替身卡不足", [], None
            if have > 1:
                inv[str(mat_type)] = have - 1
            else:
                inv.pop(str(mat_type), None)
    else:
        if mat_hero.get("id") == owner_hero.get("id"):
            return False, "不能消耗自己", [], None
        if mat_hero.get("position", 0) > 0:
            return False, "阵位上的武将不能作为材料", [], None
        _drop_hero(mat_hero["id"])
    if coin_cost > 0:
        p["coin"] = int(p.get("coin") or 0) - coin_cost

    touched = []
    store = fate_store()
    for fid, idx, need in hit:
        rec = store.setdefault(str(fid), {"groups": {}})
        groups = rec.setdefault("groups", {})
        st = groups.setdefault(str(target_group), {"exp": 0, "complete": 0})
        st["exp"] = int(st.get("exp") or 0) + progress_add
        if need > 0 and st["exp"] >= need:
            st["exp"] = need
            st["complete"] = 1
        touched.append(fid)
    return True, "+%d 进度 -> 缘分 %s" % (progress_add, touched), touched, \
        (None if consume_item else (mat_hero or {}).get("id"))


def build_formation_data(fid=1, ftype=1, lv=1):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_formation_data(fid, ftype, lv)


def build_enemy_hero_data(hid, etype, pos, name=None, stats=None, scale=100, grade=0,
                          lv=1):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）

    ★ 改 builders 签名必须同步这里（dispatch 走的是本函数）。
    """
    from . import builders_pb
    return builders_pb.build_enemy_hero_data(hid, etype, pos, name, stats, scale,
                                             grade, lv)


def build_battle_group_data(gid, formation=1, heros=None):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_battle_group_data(gid, formation, heros)


def build_battle_hero_data(hid, pos):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_battle_hero_data(hid, pos)


def build_battle_squad_data(index, heros, power=None):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_battle_squad_data(index, heros, power)


def build_battle_gameplay_data(category, squads):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_battle_gameplay_data(category, squads)


def build_squad_power_change(power=None):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_squad_power_change(power)


def build_level_record_data(lid, rec):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_level_record_data(lid, rec)


def build_award_spec(type_id, value=1, name=""):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_award_spec(type_id, value, name)


def build_award_result(type_name, specs):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）"""
    from . import builders_pb
    return builders_pb.build_award_result(type_name, specs)


# ---- 数据合规守护（对应 Hysg_Refactor 的 ensureEquipInventory）----
def _sanitize_player_data():
    """启动时校验并清理不合规的玩家数据，防止幽灵数据下发。

    规则：
      · 武将：type 必须存在于 HeroConfig
      · 装备：type 存在于 EquipConfig 且 is_equip_ownable（品质定星级区间）
      · 装备 host 必须指向存在的武将；武将 equips 与装备 host 双向一致
      · 道具：id 必须存在于 ItemConfig（Lua 配置为准，JSON 导出不完整），数量 > 0
    返回 (删除项数, 修复项数)
    """
    removed, fixed = 0, 0
    heroes = PLAYER_DATA.get("heroes", {})
    equips = PLAYER_DATA.setdefault("equips", {})
    items = PLAYER_DATA.setdefault("items", {})

    # 1) 武将
    for k in list(heroes):
        h = heroes[k]
        _row = growth.find_hero_row(h.get("type"))
        # ★ 2026-11-02：占位行（dummy 19801~19805 / 随机武将 19001）必须清掉。
        #   只判 find_hero_row 不够——19801 在配置里"存在"，但 maxStarType=0，
        #   客户端 WarriorData:type 会查 HeroConfig[0] → 兜底空表 → 崩在 SkillA，
        #   表现是「打完怪不弹结算」。
        if not _row or level_run._is_placeholder_hero(_row):
            heroes.pop(k); removed += 1
            continue
        # ★ 2026-10-28：can_break 以配置为准（启动自检纠正过期存档）——
        #   无双吕布(id=97) 存档 can_break=0 导致突破后上限卡 40（客户端读配置
        #   认为可突破，两端不一致）。配置是权威，每次启动同步。
        _cb = growth.hero_can_break(h.get("type"))
        if bool(h.get("can_break")) != bool(_cb):
            h["can_break"] = _cb
            fixed += 1
    # 2) 装备
    for k in list(equips):
        e = equips[k]; t = e.get("type")
        if not growth.equip_row(t) or not growth.is_equip_ownable(t):
            equips.pop(k); removed += 1
        elif int(e.get("host") or 0) > 0 and str(e["host"]) not in heroes:
            e["host"] = 0; removed += 1
    # 3) 道具
    ic = game_config._cached_parse(os.path.join(game_config._CONFIG_DIR, 'app.config.ItemConfig.lua'))
    for k in list(items):
        # dummy 是未完成的占位道具（ItemConfig 里 105 个），不是正式道具 —— 存档里一律清掉
        if int(items[k] or 0) <= 0 or int(k) not in ic or game_config.is_dummy_item(k):
            items.pop(k); removed += 1
    # 4) 双向引用修复
    for h in heroes.values():
        h["equips"] = [x for x in h.get("equips", []) if str(x) in equips]
    for k, e in equips.items():
        host = int(e.get("host") or 0)
        if host > 0:
            hh = heroes.get(str(host))
            if hh is not None and e["id"] not in hh.get("equips", []):
                hh.setdefault("equips", []).append(e["id"]); fixed += 1
    # 5) 队伍等级上限（2026-10-29）：设计上限 60，之前扫荡经验把玩家顶到 61+
    #    （add_team_exp 误用 ExpSetConfig 最大行号 160 当上限）。压回 60 并保留经验。
    from . import level_run as _lr
    p = PLAYER_DATA.setdefault("player", {})
    lv = int(p.get("lv") or 1)
    if lv > _lr.TEAM_LV_CAP:
        rows = game_config.load_exp_set_config() or {}
        need = int((rows.get(_lr.TEAM_LV_CAP) or {}).get("TeamExp") or 0)
        p["lv"] = _lr.TEAM_LV_CAP
        if need > 0:
            p["exp"] = min(int(p.get("exp") or 0), need - 1)
        fixed += 1
        flog("[sanitize] 队伍等级 %d > 上限 %d，压回（经验保留在进度条内）" % (lv, _lr.TEAM_LV_CAP))
    # 6) LEVEL_RECORDS int/str 键去重（2026-10-29）：混用会让 repo 把同一关写成两行，
    #    撞 level_records 唯一索引 → **所有落库失败**（用户实测：领完的礼包重启回滚）。
    lv_recs = PLAYER_DATA.get("levels") or {}
    for k in [k for k in lv_recs if isinstance(k, int) and str(k) in lv_recs]:
        _merged = dict(lv_recs[str(k)])
        for _f in ("star", "leftTimes", "buyTimes"):
            _merged[_f] = max(int(_merged.get(_f) or 0), int(lv_recs[k].get(_f) or 0))
        lv_recs[str(k)] = _merged
        lv_recs.pop(k)
        fixed += 1
    if fixed:
        flog("[sanitize] LEVEL_RECORDS int/str 重复键去重 %d 条" % fixed)
    if removed or fixed:
        save_player_data(PLAYER_DATA)
        flog("[sanitize] 清理 %d 项 / 修复 %d 项" % (removed, fixed))
    return removed, fixed


# ---- 在线会话注册表（GM 推送用）----
LIVE_SESSIONS = set()


def broadcast(opcode, body):
    """推送给所有在线客户端"""
    for s in list(LIVE_SESSIONS):
        try:
            s.send(opcode, body)
        except Exception:
            LIVE_SESSIONS.discard(s)


def push_hero_attr(hero_id):
    """推单个武将的 HeroData（453）。

    ★ 上/下阵必须推：编队军旗加成只作用于 `position>0` 的武将，
    上下阵会改变这个武将「吃不吃加成」。只推 200+20416 的话，
    客户端 warriorManager 里该武将还是旧属性 —— 表现是
    「上阵后武将详情战力不变，把军旗下阵再上阵才正常」（2026-10-24 用户报障）。
    """
    h = _find_hero(hero_id)
    if not h:
        return
    broadcast(OP_UPDATE_HERO, build_hero_data(
        h["id"], h["type"], h.get("lv", 1), h.get("position", 0)))



def push_equip_host(session, eq):
    """装备属性变化后同步宿主武将：453(属性面板) + 20416(编队战力)。

    ★ 453 必须**无条件**推，不能挂「战力变化」门槛：
      洗练/解锁可能只出「增伤/减伤/韧性」这类不进战力权重的属性 ——
      power 不变但属性面板必须刷新（2026-10-28 用户报障：
      穿着的装备培养/升星/解锁/洗练后，武将属性面板不更新）。
    未穿戴（host=0）无需推；200（货币/玩家战力）各 handler 自己发。
    """
    host = _find_hero(int((eq or {}).get("host") or 0))
    if not host:
        return
    session.send(OP_UPDATE_HERO, build_hero_data(
        host["id"], host["type"], host.get("lv", 1), host.get("position", 0)))
    session.send(OP_SQUAD_POWER, build_squad_power_change())
    flush_now()      # 装备养成是最怕丢的操作，当场落库

def push_flag_bonus_change():
    """军旗加成变化后同步客户端：200 战力/货币 + 453 上阵武将 + 20416 编队战力。

    客户端总战力公式（BattleGameplaysManager:getFormPower）：
        squad.power + Σ(编队内武将的 power)
    · `squad.power` **只在收到 20416（UpdateBattleGameplaySquadPower）时更新**
      —— 军旗系列以前只推 200/453，squad.power 永远停在登录值 → 主界面/编队
      战力不动（2026-10-24 用户报障："军旗上下阵战力不变，重上一次才变"）。
    · 上阵武将的 HeroData 由 453 更新（军旗加成体现在 position>0 的武将身上）。
    · 200 负责主界面货币/玩家战力字段。

    凡改变「上阵武将属性」或「编队级加成」的操作都必须调本函数。
    """
    broadcast(OP_UPDATE_CHARACTER, update_character_body())
    for h in PLAYER_DATA.get("heroes", {}).values():
        if int(h.get("position") or 0) > 0:
            broadcast(OP_UPDATE_HERO, build_hero_data(
                h["id"], h["type"], h.get("lv", 1), h.get("position", 0)))
    broadcast(OP_SQUAD_POWER, build_squad_power_change())


def gm_grant_item(item_id, count):
    """发放道具：卡类逐张建实例（401 由 add_item 广播）/ 普通道具堆叠并广播 451。"""
    item_id = int(item_id)
    count = int(count)
    add_item(item_id, count)          # 卡类内部已广播 401
    save_player_data(PLAYER_DATA)
    held = item_count(item_id)
    if not game_config.is_card_item(item_id):
        broadcast(451, build_item_data(item_id, held))
    return "发放道具 %s ×%d（现有 %d）" % (item_id, count, held)


def gm_grant_equip(equip_type, count=1):
    """发放装备：创建实例 + 存档 + 逐件广播 403"""
    if not growth.equip_row(equip_type) or not growth.is_equip_ownable(equip_type):
        return "装备 type=%s 无效或不可拥有" % equip_type
    eqs = PLAYER_DATA.setdefault("equips", {})
    top = max([int(k) for k in eqs] or [0])
    for _ in range(int(count)):
        top += 1
        eqs[str(top)] = {"id": top, "type": int(equip_type), "host": 0,
                         "strengthen": 0, "grade": 0, "grade_exp": 0}
        _record_equip_collect(equip_type, top)
        broadcast(403, build_equip_data(eqs[str(top)]))
    save_player_data(PLAYER_DATA)
    return "发放装备 type=%s ×%d（现有 %d 件）" % (equip_type, count, len(eqs))


def gm_set_vip(vip):
    """设置 VIP 并广播（强化暴击倍率/概率随 VIP 变化）"""
    PLAYER_DATA.setdefault("player", {})["vip"] = max(0, min(15, int(vip)))
    save_player_data(PLAYER_DATA)
    broadcast(200, update_character_body())
    return "VIP=%s" % PLAYER_DATA["player"]["vip"]


def gm_probe453(hero_id=None):
    """[train-debug] 广播一个带特征 exp=2121 的 UpdateHero，验证 453 落库链路。"""
    _ph = _find_hero(int(hero_id)) if hero_id else (_find_hero(51) or _find_hero(50) or _find_hero(1))
    if not _ph:
        return "武将不存在"
    _pid = int(_ph.get("id") or 0)
    _hb = build_hero_data(_pid, _ph["type"], _ph.get("lv", 1), _ph.get("position", 0), exp_override=2121)
    flog("[train-debug] GM 推453探针: hero=%s type=%s lv=%s exp=2121 size=%d" % (
        _pid, _ph["type"], _ph.get("lv"), len(_hb)))
    broadcast(OP_UPDATE_HERO, _hb)
    return "已广播 453 探针 hero=%s exp=2121" % _pid


def gm_set_resource(coin=None, diamond=None, lv=None, add_diamond=None):
    """设置资源并广播。add_diamond=N 为增量发放（避免覆盖游玩中的最新值）"""
    p = PLAYER_DATA.setdefault("player", {})
    if coin is not None: p["coin"] = max(0, int(coin))
    if diamond is not None: p["diamond"] = max(0, int(diamond))
    if add_diamond is not None:
        p["diamond"] = max(0, int(p.get("diamond") or 0) + int(add_diamond))
    if lv is not None: p["lv"] = max(1, int(lv))
    save_player_data(PLAYER_DATA)
    broadcast(200, update_character_body())
    return "资源: lv=%s vip=%s coin=%s diamond=%s" % (
        p.get("lv"), p.get("vip"), p.get("coin"), p.get("diamond"))


def gm_set_total_pay(value):
    """把**本轮**累计充值额直接设为 value（数据修复用）。

    2026-10-30：switch_player 换对象导致 round_pay 从未落库（玩家买了十几个
    648 计数还是老值），历史额度已无法从存档还原，只能按真实支付额
    `player.pay_money` 补一次。不改 claimed —— 已领档位保持已领。
    """
    from .activities import grade_pay
    st = grade_pay._st("total_pay")
    st["round_pay"] = max(0, int(value or 0))
    save_player_data(PLAYER_DATA)
    flush_now()
    return "本轮累计充值已设为 %d（当前轮次 key=%s）" % (st["round_pay"], st.get("round_key"))


GM_COMMAND_HELP = (
    "GM命令台可用命令：\n"
    "add_item 道具ID [数量]\nactivity_reset 活动名（重置活动进度，调试用）\n"
    "set_total_pay 金额（直接设本轮累计充值额，数据修复用）\n"
    "challenge_reset（清空挑战副本今日次数，圣兽/金银岛/阵营调试用）\n"
    "add_equip 装备type [数量]\n"
    "add_hero 武将type [数量]\n"
    "add_flag_part 部件ID [数量] | add_flag_suit 套装Group（一整套 Part1-5）\n"
    "add_name_card 数量（改名卡，CharacterData 字段 35）\n"
    "send_mail 标题 [道具ID 数量]（发一封邮件给当前角色）\n"
    "add_diamond 数量 | add_coin 数量\n"
    "set_diamond 数量 | set_coin 数量\n"
    "set_lv 等级 | set_vip 等级\n"
    "set_lottery_discount 折扣百分比 次数（100/0=关闭）\n"
    "collect_fill 1|2 分组（1=武将Origin 2=装备Part，测试图鉴用）\n"
    "help 显示本帮助"
)


def gm_console_execute(text):
    """解析 GM 命令文本（设置界面"激活码"输入框），返回结果文本。

    命令语法：`add_item 道具ID 数量` 这种空格分隔形式（2026-10-22 用户需求）。
    执行走现成 gm_* 函数；异常转为可读提示。
    """
    parts = (text or "").strip().split()
    if not parts:
        return GM_COMMAND_HELP
    cmd, args = parts[0].lower(), parts[1:]
    try:
        if cmd == "help":
            return GM_COMMAND_HELP
        if cmd == "activity_reset":
            name = parts[1] if len(parts) > 1 else ""
            st = PLAYER_DATA.get("activity", {}).pop(name, None)
            save_player_data(PLAYER_DATA)
            return "已重置活动进度：%s（原：%s）" % (name or "(空)", "有数据" if st else "无数据")
        if cmd == "challenge_reset":
            st = PLAYER_DATA.pop("challenge", None)
            save_player_data(PLAYER_DATA)
            return "已清空挑战次数（原：%s）；圣兽 %s 次 / 金银岛 %s 次 / 本日阵营 %s" % (
                st or "无数据", challenge.therion_left(), challenge.coin_left(),
                challenge.CAMP_NAMES.get(challenge.camp_act_today(), "?"))
        if cmd == "set_total_pay":
            if not args:
                return "用法：set_total_pay 金额（本轮累计充值额直接设值）"
            return gm_set_total_pay(int(args[0]))
        if cmd == "add_item":
            return gm_grant_item(int(args[0]), int(args[1]) if len(args) > 1 else 1)
        if cmd == "add_equip":
            return gm_grant_equip(int(args[0]), int(args[1]) if len(args) > 1 else 1)
        if cmd == "add_hero":
            return gm_grant_hero(int(args[0]), int(args[1]) if len(args) > 1 else 1)
        if cmd == "add_flag_part":
            return gm_grant_flag_part(int(args[0]), int(args[1]) if len(args) > 1 else 1)
        if cmd == "add_flag_suit":
            return gm_grant_flag_suit(int(args[0]))
        if cmd == "send_mail":
            # send_mail <标题> <道具id> <数量>  —— 给当前玩家发一封带道具的邮件
            title = parts[1] if len(parts) > 1 else "系统邮件"
            tid = int(parts[2]) if len(parts) > 2 else 0
            cnt = int(parts[3]) if len(parts) > 3 else 1
            awards = [{"kind": "object", "type": tid, "count": cnt}] if tid > 0 else []
            mid = mail.send_mail(title, "（来自 GM 控制台）", awards)
            save_player_data(PLAYER_DATA)
            push_mail(mid)      # 在线客户端立刻收到 300 NewMail
            return "已发送邮件 #%s：%s（%s×%s）" % (mid, title, tid or "无附件", cnt if tid > 0 else 0)
        if cmd == "add_name_card":
            n = int(args[0]) if args else 1
            p = PLAYER_DATA.setdefault("player", {})
            p["change_name_card"] = max(0, int(p.get("change_name_card") or 0) + max(0, n))
            save_player_data(PLAYER_DATA)
            return "改名卡 +%d，现有 %d 张" % (max(0, n), int(p["change_name_card"]))
        if cmd == "add_diamond":
            return gm_set_resource(add_diamond=int(args[0]))
        if cmd == "add_coin":
            cur = int(PLAYER_DATA.get("player", {}).get("coin") or 0)
            return gm_set_resource(coin=cur + max(0, int(args[0])))
        if cmd == "set_coin":
            return gm_set_resource(coin=int(args[0]))
        if cmd == "set_diamond":
            return gm_set_resource(diamond=int(args[0]))
        if cmd == "set_lv":
            return gm_set_resource(lv=int(args[0]))
        if cmd == "set_vip":
            return gm_set_vip(int(args[0]))
        if cmd == "set_lottery_discount":
            pct = int(args[0]) if args else 100
            times = int(args[1]) if len(args) > 1 else 0
            return gm_set_lottery_discount(pct, times)
        if cmd == "collect_fill":
            return gm_collect_fill(int(args[0]), int(args[1]))
        return "未知命令: %s\n%s" % (cmd, GM_COMMAND_HELP)
    except (IndexError, ValueError) as e:
        return "命令格式错误（%s）\n%s" % (e, GM_COMMAND_HELP)


def gm_grant_hero(hero_type, count=1):
    """GM 发放武将：创建实例 + 存档 + 逐只广播 404 NewHero（客户端 onNewWarrior 入库）。

    派生字段（star/avatar/spine/can_break）对齐 _refresh_hero_type 的推导方式。
    """
    hero_type = int(hero_type)
    hc = game_config.load_hero_config().get(hero_type)
    if not hc:
        return "武将 type=%s 不存在" % hero_type
    heroes = PLAYER_DATA.setdefault("heroes", {})
    top = max([int(k) for k in heroes] or [0])
    granted = []
    for _ in range(int(count)):
        top += 1
        hero = {
            "id": top, "type": hero_type, "lv": 1, "exp": 0,
            "grade": 0, "grade_exp": 0, "awaken": 0,
            "position": 0,  # 未上阵（position 必须 0，不是 -1）
            "name": game_config.get_hero_name(hero_type),
        }
        try:
            st = game_config.get_hero_star_info(hero_type)
            hero["star"] = int(st.get("quality") or hc.get("Quality") or 1)
            hero["avatar_type"] = st.get("avatar_type")
            av = game_config.get_hero_avatar(hero_type)
            hero["spine"] = av.get("spine")
            hero["avatar"] = av.get("avatar")
        except Exception as e:
            hero["star"] = int(hc.get("Quality") or 1)
            flog("[gm] grant_hero 派生字段退化: %s" % e)
        hero["can_break"] = 1 if growth.hero_can_break(hero_type, hero["star"]) else 0
        heroes[str(top)] = hero
        granted.append(top)
        _record_hero_collect(hero_type, top)
        broadcast(404, build_hero_data(top, hero_type, 1, 0))  # 404 NewHeroRt
    save_player_data(PLAYER_DATA)
    return "发放武将 %s(type=%s) ×%d（实例 id: %s）" % (
        game_config.get_hero_name(hero_type), hero_type, count, granted)


def gm_grant_flag_part(part_type, count=1):
    """GM 发放军旗部件（不扣材料）。

    部件不是道具，走 5346 FlagPartData 广播 —— 客户端
    `EnsignManger:onFlagPartProductHandler` 会 decode 后塞进 `_ensignPartList`
    并发 EnsignPartMakeSuccess 事件，列表自然刷新。
    （不要走 401/451，那是道具/装备通道，客户端不认识军旗部件。）
    """
    part_type = int(part_type)
    cfg = game_config.flag_part_cfg(part_type)
    if not cfg:
        return "军旗部件 type=%s 不存在" % part_type
    store = ensign.part_store()
    granted = []
    for _ in range(max(1, int(count))):
        pid = ensign._next_part_id()
        adds = ensign.build_additions(cfg)
        part = {
            "id": pid,
            "type": part_type,
            "host": ensign.HOST_NONE,
            "unlock_count": len(adds),
            "up_count": 0,
            "additions": adds,
        }
        store[str(pid)] = part
        granted.append(pid)
        broadcast(OP_FLAG_PART_PRODUCT, build_flag_part_data(part))
    save_player_data(PLAYER_DATA)
    return "发放军旗部件 type=%s ×%d（id: %s）" % (part_type, len(granted), granted)


def gm_grant_flag_suit(group_id):
    """GM 按套装 Group 发一整套军旗部件（Part1-5 各一件）。"""
    group_id = int(group_id)
    cfgs = game_config.load_flag_part_config()
    rows = [c for c in cfgs.values() if int(c.get("Group") or 0) == group_id]
    if not rows:
        return "没有 Group=%s 的军旗部件" % group_id
    rows.sort(key=lambda c: int(c.get("Part") or 0))
    detail = []
    for c in rows:
        pid = int(c.get("ID"))
        detail.append("Part%s(id=%s)" % (c.get("Part"), pid))
        gm_grant_flag_part(pid, 1)
    return "套装 Group=%s 发放 %d 件：%s" % (group_id, len(rows), " ".join(detail))


def build_item_data(item_id, count, type_id=None):
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）

    卡类实例用 type_id 传卡配置 id（id=唯一实例 id、type=卡 type、count=1）。
    """
    from . import builders_pb
    return builders_pb.build_item_data(item_id, count, type_id)


def build_enter_game():
    """（原生 protobuf 实现见 builders_pb；此处委托，黄金测试保证语义等价）

    独立调用（不经 dispatch）时也要给出干净缓存：一次构建内 512 个武将
    共享 on_field_groups / hero_attrs 的计算结果。
    """
    req_cache_clear()
    from . import builders_pb
    return builders_pb.build_enter_game()


# ----------------------------------------------------------------------
# 军旗系统（2026-10-23）：给 builders_pb 的快照入口
# ----------------------------------------------------------------------

def ensign_flags_snapshot():
    from . import ensign
    return [dict(v) for v in ensign.flag_store().values()]


def ensign_parts_snapshot():
    from . import ensign
    return [dict(v) for v in ensign.part_store().values()]


def ensign_warbook(data=None):
    from . import ensign
    return ensign.warbook_store(data)


def ensign_get_squad_flag(category, index):
    from . import ensign
    return ensign.get_squad_flag(category, index)


def build_flag_data(flag):
    from . import builders_pb
    return builders_pb.build_flag_data(flag)


def build_flag_part_data(part):
    from . import builders_pb
    return builders_pb.build_flag_part_data(part)


def build_update_squad_flag_datas(entries):
    from . import builders_pb
    return builders_pb.build_update_squad_flag_datas(entries)


def build_update_squad_flag_data(category, cpm, index, heros, power, flag):
    from . import builders_pb
    return builders_pb.build_update_squad_flag_data(category, cpm, index,
                                                    heros, power, flag)


def build_divination_data(event, bagua):
    from . import builders_pb
    return builders_pb.build_divination_data(event, bagua)


def build_divination_result(event, divination_count):
    from . import builders_pb
    return builders_pb.build_divination_result(event, divination_count)


def push_warbook(session):
    """跑商状态变化后推 5371（客户端面板开着会自动 refresh）。"""
    from . import builders_pb
    session.send(OP_UPDATE_WARBOOK, builders_pb.build_warbook_record_data())


def build_warbook_record_data(data=None):
    from . import builders_pb
    return builders_pb.build_warbook_record_data(data)


# ----------------------------------------------------------------------
# 招募 / 寻宝（5040/5041）
#
# 客户端契约（详见 work/招募_客户端与配置摸底.md）：
#   · 5040 请求 id 是 LotteryRecordData.id（记录主键），不是配置 type；
#   · 武将结果回 476 AllHeroMessage（key=404），寻宝结果回 5040 AwardResult；
#   · 记录里 nextFreeTime 是**剩余秒数**；totalTimes 是「该 type 抽了几次」；
#   · 招募：101 单抽 / 102 美酒单抽 / 103 美酒十连 / 104 元宝十连 / 129 招贤令十连；
#   · 寻宝：201 内海单抽 / 202 远洋单抽 / 203 远洋十连 / 204 内海十连 / 205 远洋令十连；
#   · 20 抽保底按 Group 共用计数（元宝线 Group=1 含 101/104/129），第 20 抽替换为
#     SpecialAwards 池（1030=42 个 5★ 名将）；首次十连固定换 1 个 6★（池 1004 集合）。
# ----------------------------------------------------------------------

_SIX_STAR_POOL = lottery_config.SIX_STAR_POOL


def _lottery_store():
    """确保 PLAYER_DATA['player']['lottery'] 有全部记录并补齐缺省字段。"""
    p = PLAYER_DATA.setdefault("player", {})
    store = p.setdefault("lottery", {})
    for t in lottery_config.LOTTERY_TYPES:
        rec = store.get(str(t))
        if not isinstance(rec, dict):
            rec = {}
        rec.setdefault("id", t)
        rec.setdefault("type", t)
        rec.setdefault("next_free", 0)
        rec.setdefault("total_times", 0)
        rec.setdefault("first_done", 0)
        rec.setdefault("discount", 100)
        rec.setdefault("discount_end", 0)
        rec.setdefault("discount_left", 0)
        store[str(t)] = rec
    return store


def lottery_records_snapshot():
    """给 EnterGame / 5041 的记录列表（顺序固定，便于对日志）。"""
    store = _lottery_store()
    out = []
    for t in lottery_config.LOTTERY_TYPES:
        rec = store.get(str(t))
        if rec is not None:
            out.append(rec)
    return out


def lottery_discount_info():
    """折扣活动状态（当前只作用于 104 元宝十连，客户端也只有那里有折扣 UI）。"""
    p = PLAYER_DATA.setdefault("player", {})
    d = p.setdefault("lottery_discount", {})
    d.setdefault("is_start", 0)
    d.setdefault("discount", 100)
    d.setdefault("times", 0)
    d.setdefault("left", 0)
    d.setdefault("end", 0)
    d.setdefault("type", 104)
    return d


def push_lottery_activity(session=None):
    """推送 5318 LotteryActivityInfo（登录后 / GM 改折扣时）。

    即使没有折扣也发一次（isStart=0）：客户端 EmployeePanel:setData 会读
    activityManager:getLotteryActivityData().isStart，先给它一个真实解码表，
    避免依赖 pbc「空 message 返回惰性表」的隐式行为。
    """
    from . import builders_pb
    body = builders_pb.build_lottery_activity_info()
    if session is not None:
        session.send(OP_LOTTERY_ACTIVITY, body)
    else:
        broadcast(OP_LOTTERY_ACTIVITY, body)


def gm_set_lottery_discount(percent, times):
    """GM：开启/关闭抽卡折扣（十连价 = Price × percent / 100）。

    percent=100 或 times=0 表示关闭。写进玩家存档并广播 5318 + 5041，
    招募面板上的划线价/折扣次数立即更新。
    """
    pct = max(1, min(100, int(percent)))
    cnt = max(0, int(times))
    info = lottery_discount_info()
    info["is_start"] = 1 if (cnt > 0 and pct < 100) else 0
    info["discount"] = pct
    info["times"] = cnt
    info["left"] = cnt if info["is_start"] else 0
    rec = _lottery_store().get("104")
    if rec is not None:
        rec["discount"] = pct if info["is_start"] else 100
        rec["discount_left"] = info["left"]
    save_player_data(PLAYER_DATA)
    broadcast(OP_LOTTERY_RECORDS, builders_lottery_records())
    push_lottery_activity()
    return "抽卡折扣：%s（%d%%，剩余 %d 次）" % (
        "开启" if info["is_start"] else "关闭", pct, info["left"])


def gm_collect_fill(collection_type, sub_type):
    """GM：把某组所有可收集 type 点亮（测试图鉴进度/领奖；重登后客户端生效）。

    collect_fill 1 2  → 点亮全部蜀国武将
    collect_fill 2 1  → 点亮全部武器
    """
    ctype, subtype = int(collection_type), int(sub_type)
    c = _collect_store()
    if ctype == 1:
        types = sorted(t for t, m in collect_config.hero_collect_map().items()
                       if m.get("origin") == subtype)
        c["heroes"] = sorted(set(c["heroes"]) | set(types))
    elif ctype == 2:
        types = sorted(t for t, m in collect_config.equip_collect_map().items()
                       if m.get("part") == subtype)
        c["equips"] = sorted(set(c["equips"]) | set(types))
    else:
        return "type 只能是 1(武将)/2(装备)"
    save_player_data(PLAYER_DATA)
    return "已点亮 type=%d subType=%d 共 %d 个（重新登录后图鉴生效）" % (
        ctype, subtype, len(types))


def builders_lottery_records():
    from . import builders_pb
    return builders_pb.build_lottery_records(lottery_records_snapshot())


def _lottery_ref_key(ref):
    if isinstance(ref, int):
        return ("pool", ref)
    if not isinstance(ref, dict):
        return ("raw", str(ref))
    return (ref.get("ClassName"), ref.get("AwardID"), ref.get("Type"), ref.get("Count"))


def _lottery_same_refs(a, b):
    return [_lottery_ref_key(x) for x in a] == [_lottery_ref_key(x) for x in b]


def _lottery_refs_differ(a, b):
    return not _lottery_same_refs(a, b)


def _lottery_group_drawn(group):
    """该 Group 已累计抽了多少次（totalTimes × Count）。"""
    total = 0
    cfg_all = lottery_config.lottery_set()
    if int(group or 0) <= 0:
        return 0
    for rec in _lottery_store().values():
        cfg = cfg_all.get(int(rec.get("type") or 0))
        if not cfg:
            continue
        if int(cfg.get("Group") or 0) == int(group):
            total += int(rec.get("total_times") or 0) * int(cfg.get("Count") or 1)
    return total


def _lottery_first_special(cfg, rec, count):
    """首次抽卡/首次十连的特殊奖池。

    返回 ("six_star", None) 或 ("pool", [ref...]) 或 None。
    · 104 配的是 FirstNationAwards（4 国池）——用户口径「总池任意一个 6★」，
      实际按 1004 池的 6★ 集合随机（42 人）；
    · 101 首抽 → 2003（4★ 16 选 1）；102 首抽 → 固定张星彩 2★；
    · 203 首次十连 → 3320（紫装）；
    · 与普通池相同的 FirstAwards（103/129/202/204/205）不算特殊。
    """
    if int(rec.get("first_done") or 0):
        return None
    if int(count) > 1:
        fna = lottery_config.as_refs(cfg.get("FirstNationAwards"))
        if fna:
            return ("six_star", None)
    first = lottery_config.as_refs(cfg.get("FirstAwards"))
    normal = lottery_config.as_refs(cfg.get("Awards"))
    if first and _lottery_refs_differ(first, normal):
        return ("pool", first)
    return None


def _lottery_roll_six_star(rng):
    types = sorted(lottery_config.pool_hero_types(_SIX_STAR_POOL))
    if not types:
        return []
    return [{"kind": "hero", "type": int(rng.choice(types)), "count": 1}]


def _lottery_roll_awards(refs, rng):
    return lottery_config.roll_refs(refs, rng)


def _create_hero_instance(hero_type):
    """抽到的武将入库（字段对齐 gm_grant_hero / _refresh_hero_type）。"""
    hero_type = int(hero_type)
    hc = game_config.load_hero_config().get(hero_type)
    if not hc:
        raise ValueError("HeroConfig 缺少 type=%s" % hero_type)
    heroes = PLAYER_DATA.setdefault("heroes", {})
    top = max([int(k) for k in heroes] or [0]) + 1
    hero = {"id": top, "type": hero_type, "lv": 1, "exp": 0,
            "grade": 0, "grade_exp": 0, "awaken": 0,
            "position": 0,  # 未上阵必须是 0（不是 -1）
            "name": game_config.get_hero_name(hero_type)}
    # 成就计数「获得武将」（target=9）：圣兽材料（14001~14035）不算
    if not (14001 <= hero_type <= 14035):
        try:
            mission_achieve.bump_counter("hero_get")
        except Exception:
            pass
    try:
        st = game_config.get_hero_star_info(hero_type)
        hero["star"] = int(st.get("quality") or hc.get("Quality") or 1)
        hero["avatar_type"] = st.get("avatar_type")
        av = game_config.get_hero_avatar(hero_type)
        hero["spine"] = av.get("spine")
        hero["avatar"] = av.get("avatar")
    except Exception as e:
        hero["star"] = int(hc.get("Quality") or 1)
        flog("[lottery] 武将派生字段退化 type=%s: %s" % (hero_type, e))
    hero["can_break"] = 1 if growth.hero_can_break(hero_type, hero["star"]) else 0
    heroes[str(top)] = hero
    _record_hero_collect(hero_type, top)
    return top


def _create_equip_instance(equip_type):
    """抽到的装备入库（host=0 未穿戴，builders_pb.equip_msg 会显式下发 0）。"""
    equip_type = int(equip_type)
    if not growth.equip_row(equip_type) or not growth.is_equip_ownable(equip_type):
        raise ValueError("装备 type=%s 不可拥有" % equip_type)
    eqs = PLAYER_DATA.setdefault("equips", {})
    top = max([int(k) for k in eqs] or [0]) + 1
    eqs[str(top)] = {"id": top, "type": equip_type, "host": 0,
                     "strengthen": 0, "grade": 0, "grade_exp": 0}
    _record_equip_collect(equip_type, top)
    return top


def do_lottery(record_id, use_item=False):
    """执行一次招募/寻宝。

    返回 (ok, reason, result)：
      hero:    {"category":"hero",    "heroes":[(实例id, type)...], "plan":{...}}
      explore: {"category":"explore", "awards":[{kind,type,count,id?,name}], "plan":{...}}
    """
    store = _lottery_store()
    rec = store.get(str(int(record_id or 0)))
    if rec is None:
        return False, "抽卡记录不存在(%s)" % record_id, None
    ltype = int(rec.get("type") or 0)
    cfg = lottery_config.lottery_set().get(ltype)
    if not cfg:
        return False, "抽卡配置不存在(%s)" % ltype, None

    count = max(1, int(cfg.get("Count") or 1))
    hero_side = ltype in lottery_config.HERO_LOTTERY_TYPES
    now = time.time()

    # ---- 免费判定（只做了冷却：到点就能免费抽一次，抽完重新计时）----
    free_hours = int(cfg.get("FreeHour") or 0)
    is_free = free_hours > 0 and int(rec.get("next_free") or 0) <= now

    # ---- 费用方案（先算，不扣；抽失败不扣钱）----
    plan = {"free": False, "item": 0, "item_count": 0,
            "currency": "", "price": 0, "discount": False}
    if is_free:
        plan["free"] = True
        plan["next_free"] = int(now + free_hours * 3600)
    else:
        paid = False
        if use_item:
            token = (lottery_config.RECRUIT_TOKEN if hero_side
                     else lottery_config.EXPLORE_TOKEN)
            need = 10 if count >= 10 else 1
            have = int((PLAYER_DATA.get("items") or {}).get(str(token), 0))
            if have >= need:
                plan["item"] = token
                plan["item_count"] = need
                paid = True
        if not paid:
            price = int(cfg.get("Price") or 0)
            # 折扣只作用于 104（客户端只在元宝十连上有划线价/折后价 UI）
            if ltype == 104:
                info = lottery_discount_info()
                if int(info.get("is_start") or 0) and int(info.get("left") or 0) > 0:
                    price = int(price * int(info.get("discount") or 100) / 100)
                    plan["discount"] = True
            key = {1: "coin", 2: "diamond", 3: "vigour"}.get(int(cfg.get("Currency") or 0))
            if not key:
                return False, "该抽卡货币暂不支持(Currency=%s)" % cfg.get("Currency"), None
            have = int((PLAYER_DATA.get("player") or {}).get(key) or 0)
            if have < price:
                return False, "资源不足（需要 %d，现有 %d）" % (price, have), None
            plan["currency"] = key
            plan["price"] = price

    # ---- 抽（先抽后扣）----
    rng = random
    group = int(cfg.get("Group") or 0)
    drawn_before = _lottery_group_drawn(group)
    pity_slots = set()
    if group > 0:
        for i in range(1, count + 1):
            if (drawn_before + i) % 20 == 0:
                pity_slots.add(i)

    first_kind = _lottery_first_special(cfg, rec, count)
    first_slot = count if first_kind else 0
    if first_kind and first_slot in pity_slots:
        # 首次十连 6★ 与第 20 抽保底撞同一抽：6★ 优先。
        # 保底池与普通池不同时把保底挪到前一抽，避免吞掉玩家保底。
        pity_slots.discard(first_slot)
        if (_lottery_refs_differ(lottery_config.as_refs(cfg.get("SpecialAwards")),
                                 lottery_config.as_refs(cfg.get("Awards")))
                and count - 1 >= 1):
            pity_slots.add(count - 1)

    normal_refs = lottery_config.as_refs(cfg.get("Awards"))
    special_refs = lottery_config.as_refs(cfg.get("SpecialAwards"))
    awards = []
    for i in range(1, count + 1):
        if first_kind and i == first_slot:
            if first_kind[0] == "six_star":
                got = _lottery_roll_six_star(rng)
            else:
                got = _lottery_roll_awards(first_kind[1], rng)
        elif i in pity_slots:
            got = _lottery_roll_awards(special_refs or normal_refs, rng)
        else:
            got = _lottery_roll_awards(normal_refs, rng)
        if not got:
            got = _lottery_roll_awards(normal_refs, rng)
        awards.extend(got)

    if hero_side:
        hero_awards = [a for a in awards if a.get("kind") == "hero"]
        if not hero_awards:
            return False, "奖池配置异常（未产出武将）", None
        if len(hero_awards) != count:
            flog("[lottery] %s 期望 %d 个武将，实际 %d" % (ltype, count, len(hero_awards)))
    else:
        if not awards:
            return False, "奖池配置异常（未产出奖励）", None

    # ---- 扣费 ----
    if plan["free"]:
        rec["next_free"] = plan["next_free"]
    elif plan["item"]:
        items = PLAYER_DATA.setdefault("items", {})
        left = int(items.get(str(plan["item"]), 0)) - plan["item_count"]
        items[str(plan["item"])] = max(0, left)
    else:
        p = PLAYER_DATA.setdefault("player", {})
        key = plan["currency"]
        p[key] = max(0, int(p.get(key) or 0) - int(plan["price"]))
        if plan["discount"]:
            info = lottery_discount_info()
            info["left"] = max(0, int(info.get("left") or 0) - 1)
            if int(info["left"]) <= 0:
                info["is_start"] = 0
                rec["discount"] = 100
                rec["discount_left"] = 0
            else:
                rec["discount"] = int(info.get("discount") or 100)
                rec["discount_left"] = int(info["left"])

    rec["total_times"] = int(rec.get("total_times") or 0) + 1
    rec["first_done"] = 1

    # ---- 入库 ----
    result = {"category": "hero" if hero_side else "explore", "plan": plan}
    if hero_side:
        heroes = []
        for a in awards:
            if a.get("kind") != "hero":
                continue
            hid = _create_hero_instance(a["type"])
            heroes.append((hid, int(a["type"])))
        result["heroes"] = heroes
    else:
        out = []
        items = PLAYER_DATA.setdefault("items", {})
        for a in awards:
            if a.get("kind") == "equip":
                eid = _create_equip_instance(a["type"])
                out.append({"kind": "equip", "id": eid, "type": int(a["type"]),
                            "count": 1, "name": game_config.get_equip_name(a["type"])})
            else:
                t = int(a.get("type") or 0)
                c = max(1, int(a.get("count") or 1))
                if game_config.is_card_item(t):
                    # 卡类：逐张建实例（每张唯一 id），结果列表也一卡一条
                    for _ in range(c):
                        add_item(t, 1)
                        out.append({"kind": "item", "type": t, "count": 1,
                                    "name": game_config.get_item_name(t)})
                else:
                    add_item(t, c)
                    out.append({"kind": "item", "type": t, "count": c,
                                "name": game_config.get_item_name(t)})
        result["awards"] = out

    save_player_data(PLAYER_DATA)
    return True, "", result


# ----------------------------------------------------------------------
# 图鉴 / 收集（560 / 565 / 10115）
#
# 客户端 CollectManager 口径：
#   · 武将：ShowInCollect=1 的 type 是否「曾经获得过」；按 Origin 分组（SubType=Origin）
#   · 装备：ShowInCollect=1 的 type；按 Part 分组（SubType=Part=1..4）
#   · 进度 = 该组内已点亮的不同 type 数；里程碑奖励在 CollectionAwardConfig
#   · 记录要「永久」保留（武将当材料吃掉/装备分解后图鉴仍亮），所以单独存 type 列表，
#     不能只从当前背包推导。
# ----------------------------------------------------------------------

_COLLECT_HERO = 1
_COLLECT_EQUIP = 2


def _collect_store():
    """PLAYER_DATA['player']['collect'] = {heroes:[type], equips:[type], awards:{'1_1':[15]}}"""
    p = PLAYER_DATA.setdefault("player", {})
    c = p.setdefault("collect", {})
    if not isinstance(c, dict):
        c = {}
        p["collect"] = c
    c.setdefault("heroes", [])
    c.setdefault("equips", [])
    c.setdefault("awards", {})
    try:
        c["heroes"] = sorted({int(x) for x in c["heroes"]})
    except (TypeError, ValueError):
        c["heroes"] = []
    try:
        c["equips"] = sorted({int(x) for x in c["equips"]})
    except (TypeError, ValueError):
        c["equips"] = []
    if not isinstance(c["awards"], dict):
        c["awards"] = {}
    return c


def init_collection_records():
    """把当前武将/装备并进图鉴记录（兼容老存档；幂等，不落库）。

    不能只靠「获得时打点」：本功能上线前就存在的武将/装备，以及 GM 直接发的东西，
    都要在登录时补进图鉴。
    """
    c = _collect_store()
    hero_map = collect_config.hero_collect_map()
    hs = set(c["heroes"])
    for h in (PLAYER_DATA.get("heroes") or {}).values():
        try:
            t = int(h.get("type") or 0)
        except (TypeError, ValueError):
            continue
        if t in hero_map:
            hs.add(t)
    c["heroes"] = sorted(hs)

    equip_map = collect_config.equip_collect_map()
    es = set(c["equips"])
    for eq in (PLAYER_DATA.get("equips") or {}).values():
        try:
            t = int(eq.get("type") or 0)
        except (TypeError, ValueError):
            continue
        if t in equip_map:
            es.add(t)
    c["equips"] = sorted(es)
    return c


def collect_snapshot():
    """给 EnterGame 用：(hero_types, equip_types, {key: [milestones]})"""
    c = init_collection_records()
    return list(c["heroes"]), list(c["equips"]), dict(c["awards"])


def _record_hero_collect(hero_type, record_id=None):
    """点亮一个武将 type；返回是否新增（新增时推 560）。"""
    try:
        t = int(hero_type or 0)
    except (TypeError, ValueError):
        return False
    if t not in collect_config.hero_collect_map():
        return False
    c = _collect_store()
    if t in c["heroes"]:
        return False
    c["heroes"] = sorted(set(c["heroes"]) | {t})
    try:
        from . import builders_pb
        broadcast(560, builders_pb.build_hero_record(t, record_id))
    except Exception as e:
        flog("[collect] 推 560 失败 type=%s: %s" % (t, e))
    return True


def _record_equip_collect(equip_type, record_id=None):
    """点亮一个装备 type；返回是否新增（新增时推 565）。"""
    try:
        t = int(equip_type or 0)
    except (TypeError, ValueError):
        return False
    if t not in collect_config.equip_collect_map():
        return False
    c = _collect_store()
    if t in c["equips"]:
        return False
    c["equips"] = sorted(set(c["equips"]) | {t})
    try:
        from . import builders_pb
        broadcast(565, builders_pb.build_equip_record(t, record_id))
    except Exception as e:
        flog("[collect] 推 565 失败 type=%s: %s" % (t, e))
    return True


def collect_progress(collection_type, sub_type):
    """该组已点亮的不同 type 数（与客户端 getCollectionProgress 同口径）。"""
    c = init_collection_records()
    ctype, subtype = int(collection_type), int(sub_type)
    if ctype == _COLLECT_HERO:
        m = collect_config.hero_collect_map()
        return sum(1 for t in c["heroes"]
                   if m.get(int(t), {}).get("origin") == subtype)
    if ctype == _COLLECT_EQUIP:
        m = collect_config.equip_collect_map()
        return sum(1 for t in c["equips"]
                   if m.get(int(t), {}).get("part") == subtype)
    return 0


def _normalize_collect_awards(raw_awards):
    out = []
    for a in (raw_awards or []):
        if not isinstance(a, dict):
            continue
        cn = a.get("ClassName")
        if cn == "PropertyAward":
            out.append({"kind": "property",
                        "prop": a.get("PropertyName"),
                        "value": int(a.get("Value") or 0)})
        elif cn == "ObjectAward":
            out.append({"kind": "object",
                        "type": int(a.get("Type") or 0),
                        "count": int(a.get("Count") or 1)})
    return out


def _grant_collect_awards(raw_awards):
    """发放图鉴里程碑奖励。返回 (normalized_awards, desc_list)。

    头像框（HonorConfig.HonorPart=1，55025~55028）不在道具表里，单独说明；
    当前 build_honors 已把全部头像/框下发，客户端不缺，所以不做库存操作。
    """
    awards = _normalize_collect_awards(raw_awards)
    heads, frames = game_config.honor_ids()
    honor_ids = set(frames) | set(heads)
    normal = [a for a in awards
              if not (a.get("kind") == "object" and a["type"] in honor_ids)]
    desc = _grant_sign_awards(normal) if normal else []
    for a in awards:
        if a.get("kind") == "object" and a["type"] in honor_ids:
            desc.append("头像/头像框×1")
    return awards, desc


def _collection_award_specs(awards):
    """收集奖励 -> AwardSpecData 元组。头像框用 type=0(属性类) + 名称显示：

    AwardItem:setData 对 type>0 会走 getPropConfig(type).Quality；HonorConfig 没有
    Quality 字段，直接当 ObjectAward 发会取到 nil。头像框已经在 build_honors 里
    全量解锁，这里只借属性类槽位把「获得了什么」显示出来。
    """
    heads, frames = game_config.honor_ids()
    honor_ids = set(heads) | set(frames)
    specs = []
    for a in (awards or []):
        if a.get("kind") == "property":
            specs.append((0, int(a.get("value") or 0), str(a.get("prop") or "")))
        elif a.get("kind") == "object":
            t = int(a.get("type") or 0)
            c = int(a.get("count") or 1)
            if t in honor_ids:
                specs.append((0, c, game_config.get_honor_name(t)))
            else:
                specs.append((t, c))
    return specs


def do_receive_collection_award(collection_type, sub_type, milestone_count):
    """领取一个图鉴里程碑。返回 (ok, reason, result)。"""
    ctype = int(collection_type)
    subtype = int(sub_type)
    ms = int(milestone_count)
    row = collect_config.award_row(ctype, subtype, ms)
    if not row:
        return False, "奖励配置不存在（type=%s subType=%s milestone=%s）" % (ctype, subtype, ms), None
    c = _collect_store()
    key = "%d_%d" % (ctype, subtype)
    received = set(int(x) for x in (c["awards"].get(key) or []))
    if ms in received:
        return False, "该里程碑已领取", None
    progress = collect_progress(ctype, subtype)
    if progress < ms:
        return False, "收集进度不足（%d/%d）" % (progress, ms), None
    awards, desc = _grant_collect_awards(row.get("Awards") or [])
    received.add(ms)
    c["awards"][key] = sorted(received)
    save_player_data(PLAYER_DATA)
    return True, "", {"type": ctype, "subType": subtype,
                      "milestones": sorted(received), "awards": awards, "desc": desc}


# ----------------------------------------------------------------------
# 以下 dispatch 由 mock_server.py 的 Session.dispatch 机械移植（self -> session）
# ----------------------------------------------------------------------

# ---- 战斗中断标记（断线重连回主界面，2026-10-27）----
# 客户端 MyApp:onEnterGame 在 Battle* 场景收到 EnterGame 时只清掉登录数据并
# 调 socket:onReconnect()（原版用于重放积压请求续战）。本服没有战斗会话，
# 必须由服务端明确告诉客户端“这场战斗已失效”——客户端原本就会在 Battle
# 场景收到 5001 GameError(errorCode=18) 时弹提示并切回 GameScene：
#   MyApp.lua onGameError -> 若当前是 Battle 且 code==10/18 -> replaceScene(GameScene)
# 所以在 LevelBegin 等战斗开始请求上记标记，重连登录（Login.Reconnect>0）时
# 若仍有标记，就在 EnterGame 后补发 5001；客户端新开（Reconnect=0）则视为
# 主动放弃旧战斗，直接清标记。
_BATTLE_BEGIN_OPCODES = {
    5030,   # LevelBegin（PVE 关卡）
    5071,   # AttackHeroStageBegin（武将关）
    7000,   # RequestAttackArena（竞技场）
    5510,   # BossFightBegin
    5514,   # TowerFightBegin
    6007,   # ActivityLevelBegin
    30004,  # ChallengeCrossRealmWar
    20446,  # SupplyLineCutoffActivityBattleZonePlayer
    573,    # SeaBattleBegin
    5341,   # ConquerCityActivityLevelBegin
}
_BATTLE_END_OPCODES = {
    5031,   # LevelEnd
    5072,   # AttackHeroStageResult
    7001,   # AttackArenaBattleResult
    5511,   # BossFightEnd
    5512,   # BossFightQuickEnd
    5515,   # TowerFightEnd
    6008,   # ActivityLevelEnd
    30005,  # ChallengeEnd
    20447,  # AttackSupplyLineCutoffActivityBattleZonePlayerResult
    5090,   # SeaBattleResult
    5342,   # ConquerCityActivityLevelEnd
}


def _set_battle_in_progress(flag):
    """记录/清除“战斗未结算”标记（持久化，服务端重启后重连仍能识别）。"""
    cur = bool(PLAYER_DATA.get("battle_in_progress"))
    if cur == bool(flag):
        return
    if flag:
        PLAYER_DATA["battle_in_progress"] = 1
    else:
        PLAYER_DATA.pop("battle_in_progress", None)
    try:
        save_player_data(PLAYER_DATA)
    except Exception:
        pass


def on_login_packet(reconnect):
    """登录首包：Reconnect=0 表示客户端新开（重启），旧战斗视为主动放弃。"""
    try:
        rc = int(reconnect or 0)
    except (TypeError, ValueError):
        rc = 0
    if rc <= 0:
        _set_battle_in_progress(False)


def push_game_error(session, code, msg):
    """5001 GameError{errorCode, msg}。"""
    from ..proto import ps_protobuf_pb2 as _pb
    m = _pb.GameError()
    m.errorCode = int(code)
    m.msg = str(msg or "")
    session.send(5001, m.SerializeToString())


# ----------------------------------------------------------------------
# 战报（BattleRecord）缓存（2026-10-29）
#
# 客户端链路（实读 app.view_zh_CN.battle.result.BattleResultPanel / ArenaManager）：
#   结算面板 _btnZhanBao → arenaManager:sendToReviewAttack(self._battleID)
#   → send("ShowBattle", 5070, {key=self._battleID})
#   → 回包 socket:decode("AttackArena") → newUIManager:addPanel(BattleRecordPanel,
#     {initParams = {result = <AttackArena>}})
#   → BattleRecordPanelData 读 result.battleRecord（BattleRecordData）+ result.result
#     + 每个 HeroData 的 recordDamage/recordHealing/recordDamageTaken/... (proto 42~49)
#
# 服务端职责：
#   ① LevelResult 带 battleID（字段 7）——客户端拿它当 ShowBattle 的 key；
#   ② 把 LevelEnd(5031) 上传的 BattleRecordData **原样存下来**（客户端已算好全部统计）；
#   ③ 5070 回 AttackArena{id, result, battleRecord, recordKey}。
#
# 记录只在「刚打完的结算界面」用得到，存进程内存环形缓冲即可（不落库，重启丢失可接受）。
# ----------------------------------------------------------------------

_BATTLE_RECORD_LIMIT = 32
_BATTLE_RECORDS = {}          # key -> {"raw": bytes, "lid", "result", "star", "use_time"}
_BATTLE_RECORD_SEQ = [0]
_BATTLE_RECORD_LAST = [None]  # 最近一条的 key（客户端 key 为空时兜底）


def remember_battle_record(lid, record_msg, result_val, star, use_time):
    """把客户端上传的 BattleRecordData 存进环形缓冲，返回可下发的 battleID。"""
    if record_msg is None:
        return ""
    try:
        raw = record_msg.SerializeToString()
    except Exception as e:
        flog("[battle-record] 序列化失败（%r）" % e)
        return ""
    if not raw:
        return ""
    _BATTLE_RECORD_SEQ[0] += 1
    key = "PVE%d-%d-%d" % (int(lid), int(time.time()) & 0xFFFFFF, _BATTLE_RECORD_SEQ[0])
    _BATTLE_RECORDS[key] = {
        "raw": raw,
        "key": key,
        "lid": int(lid),
        "result": int(result_val or 0),
        "star": int(star or 0),
        "use_time": int(use_time or 0),
    }
    _BATTLE_RECORD_LAST[0] = key
    while len(_BATTLE_RECORDS) > _BATTLE_RECORD_LIMIT:
        _BATTLE_RECORDS.pop(next(iter(_BATTLE_RECORDS)))
    flog("[battle-record] 已存战报 key=%s lid=%s result=%s bytes=%d"
         % (key, lid, result_val, len(raw)))
    return key


def get_battle_record(key):
    """按 battleID 取战报；key 为空/查不到时回退到最近一条（客户端在结算界面立刻点）。"""
    rec = _BATTLE_RECORDS.get(str(key or ""))
    if rec is not None:
        return rec
    last = _BATTLE_RECORD_LAST[0]
    return _BATTLE_RECORDS.get(last) if last else None


def ep_display():
    """体力三件套（CharacterData 18/29/30）：当前值 / 上限 / 下一点到达时间戳。"""
    ep = economy.sync(PLAYER_DATA)
    return {"ep": int(ep), "max": int(economy.ep_max(PLAYER_DATA)),
            "next": int(economy.next_get_time(PLAYER_DATA))}



def _challenge_level_end(session, opcode, lid, result_val, star, tmsg):
    """挑战副本结算（5031 / 6008 共用）。2026-10-29

    与主线关卡的三点区别：
      · 奖励按副本类型走 challenge.settle：圣兽=升星材料、金银岛=伤害换铜钱、
        阵营=随机 1 只武将（难度 4~5 出 Q6）
      · 次数在**结算**扣（失败/掉线不扣），回包带 ActivityTherionTimes(9) /
        ActivityCoinLevelTimes(10) 让客户端直接刷新剩余次数
      · 金银岛时间到（result=0）也按伤害发铜钱 —— 这是玩法本身（打伤害换钱）

    战报（battleID）、星级、队伍经验、武将经验与主线保持一致。
    """
    _br = getattr(tmsg, "battleRecord", None) if tmsg is not None else None
    _mg = getattr(_br, "myGroup", None) if _br is not None else None
    _rep_ids = [h.id for h in (_mg.heros if _mg is not None else [])]
    _total_damage = getattr(tmsg, "totalDamage", None) if tmsg is not None else None
    win = int(result_val or 0) >= 1

    def _zero_body(reason):
        _z = b""
        _z += f_int(1, lid)
        _z += f_int(2, 0)      # teamExp
        _z += f_int(3, 0)      # coin
        _z += f_int(4, 0)      # exp
        session.send(opcode, _z)
        if reason:
            session.send(5002, f_int(1, 0) + f_str(2, reason))

    # ---- 战斗结果合理性校验（胜利才校验；金银岛发奖不依赖胜负）----
    if win:
        _owner = {int(h.get("id") or 0) for h in PLAYER_DATA.get("heroes", {}).values()}
        _sp = 0
        for _h in PLAYER_DATA.get("heroes", {}).values():
            if int(_h.get("id") or 0) in set(_rep_ids):
                _st = hero_attrs(_h.get("type"), _h.get("lv", 1), _h.get("grade", 0))
                apply_fate_to_stats(_st, _h, ensign.squad_flag_bonuses_for_hero(_h.get("type")))
                _sp += int(_st.get("power") or 0)
        _ok, _rule, _reason = battle_validate.validate_level_end(game_config, {
            "lid": lid,
            "result": result_val,
            "star": star,
            "total_damage": _total_damage,
            "use_time": getattr(tmsg, "useTime", None) if tmsg is not None else None,
            "my_die_count": getattr(_br, "myDieCout", None) if _br is not None else None,
            "my_group_ids": _rep_ids,
            "my_group_power": getattr(_mg, "power", None) if _mg is not None else None,
            "server_power": _sp,
            "owner_hero_ids": _owner,
        })
        if not _ok:
            session.log("挑战结算校验拒绝[%s]: %s (lid=%s)" % (_rule, _reason, lid))
            _zero_body("战斗结果未通过校验：%s" % _reason)
            return
        session.log("挑战结算校验通过[%s]: %s" % (_rule, _reason))

    # ---- 战报（客户端「战报」按钮靠 battleID 取回记录）----
    _battle_id = remember_battle_record(
        lid, _br, result_val, star,
        getattr(tmsg, "useTime", None) if tmsg is not None else None)

    # ---- 发奖 + 扣次数 ----
    pay = challenge.settle(lid, win, _total_damage)
    if pay.get("reason"):
        session.log("  挑战结算不发奖：%s" % pay["reason"])
    _p = PLAYER_DATA.setdefault("player", {})
    if pay.get("coin"):
        _p["coin"] = int(_p.get("coin") or 0) + int(pay["coin"])
    _desc = _grant_sign_awards(pay["awards"]) if pay.get("awards") else []
    if pay.get("awards"):
        _push_granted_items(session, pay["awards"])
        _push_new_heroes(session, pay["awards"])
    if _desc:
        session.log("  挑战奖励：铜币×%d %s" % (pay.get("coin") or 0, "、".join(_desc)))

    # ---- 队伍经验 + 武将经验（与主线同口径）----
    hero_exp = growth.team_exp()
    _tl, _te, _nl, _ne = level_run.add_team_exp(100)
    if _nl != _tl:
        session.log("  队伍升级 %d -> %d" % (_tl, _nl))
    changes = []
    if win:
        on_field = sorted(
            [h for h in PLAYER_DATA.get("heroes", {}).values()
             if int(h.get("position") or 0) > 0],
            key=lambda x: int(x.get("position") or 0))[:5]
        for h in on_field:
            row = growth.find_hero_row(h.get("type"))
            if not row:
                continue
            q = int(growth._num(row.get("Quality")))
            cap = growth.level_cap(q, h.get("awaken", 0), h.get("can_break"))
            old_lv, old_exp = int(h.get("lv") or 1), int(h.get("exp") or 0)
            nl, ne, ups, wasted = growth.add_exp(
                q, old_lv, old_exp, hero_exp, cap, h.get("can_break"))
            h["lv"], h["exp"] = nl, ne
            changes.append((h["id"], old_exp, old_lv, ne, nl,
                            0 if wasted >= hero_exp else hero_exp))

    # ---- 成就计数：击杀 / BOSS（挑战关胜利也算征战，2026-10-29）----
    if win:
        try:
            mission_achieve.bump_counter("kill", len(game_config.get_level_enemies(lid) or []))
            _crow = challenge.level_row(lid) or {}
            if int(_crow.get("IsBoss") or 0) >= 2:
                mission_achieve.bump_counter("boss")
        except Exception as _e:
            flog("[mission] 挑战计数失败: %r" % _e)

    # ---- 星级（服务端定星，只升不降；客户端按星解锁阵营难度）----
    _star = 0
    if win:
        _die = getattr(_br, "myDieCout", None) if _br is not None else None
        try:
            _die = int(_die)
        except (TypeError, ValueError):
            _die = None
        if _die is None:
            _star = 1
        elif _die == 0:
            _star = 3
        elif _die == 1:
            _star = 2
        else:
            _star = 1
    _old = LEVEL_RECORDS.get(lid) or LEVEL_RECORDS.get(str(lid)) or {}
    _star = max(_star, int(_old.get("star") or 0))
    _left = challenge.remaining_for_level(lid) or 0
    LEVEL_RECORDS[str(lid)] = {"star": _star, "leftTimes": _left, "buyTimes": 0}

    # ---- LevelResult ----
    body = b""
    body += f_int(1, lid)
    body += f_int(2, 100)                       # teamExp
    body += f_int(3, int(pay.get("coin") or 0))  # coin（金银岛=伤害换的铜钱）
    body += f_int(4, hero_exp)                  # 每个上阵武将经验
    if _battle_id:
        body += f_str(7, _battle_id)
    for cid, oexp, olv, nexp, nlv, add in changes:
        exp_chg = b""
        exp_chg += f_int(1, cid)
        exp_chg += f_int(2, oexp)
        exp_chg += f_int(3, olv)
        exp_chg += f_int(4, nexp)
        exp_chg += f_int(5, nlv)
        exp_chg += f_int(6, add)
        body += f_msg(5, exp_chg)
    for _a in (pay.get("awards") or []):
        body += f_msg(6, _award_spec_bytes(_a.get("type"), _a.get("count"), extra=True))
    if pay.get("therion_left") is not None:
        body += f_int(9, int(pay["therion_left"]))    # ActivityTherionTimes
    if pay.get("coin_left") is not None:
        body += f_int(10, int(pay["coin_left"]))      # ActivityCoinLevelTimes
    session.send(opcode, body)

    # ---- 关卡记录（星级 / 剩余次数）----
    rec = b""
    rec += f_int(1, lid)
    rec += f_int(2, lid)
    rec += f_int(3, _star)
    rec += f_int(4, _left)
    rec += f_int(5, 0)
    session.send(520, rec)

    save_player_data(PLAYER_DATA)
    session.send(OP_UPDATE_CHARACTER, update_character_body())
    session.send(OP_SQUAD_POWER, build_squad_power_change())
    try:
        mission_achieve.push_updates(session)
    except Exception as _e:
        flog("[mission] 挑战成就刷新失败: %r" % _e)
    session.log("挑战结算完成：star=%s 剩余次数=%s" % (_star, _left))


def dispatch(session, opcode, pb):
    # typed-first：未映射/解析失败的 opcode 由助手自动回退字节级
    # （等价性由 tools/golden_test.py 双路比对保证）
    req_cache_clear()      # 请求级缓存边界：一次请求内的重复计算才复用
    _HERO_404_SEEN.clear()  # 404 去重同样按请求重置
    try:
        mission_periodic.dispatch_tick(session)   # 三餐窗口开合 / 跨天刷新（30s 节流）
    except Exception:
        pass
    tmsg = parse_typed(opcode, pb)
    if opcode in _BATTLE_BEGIN_OPCODES:
        _set_battle_in_progress(True)
    elif opcode in _BATTLE_END_OPCODES:
        _set_battle_in_progress(False)
    # ---- 挑战副本查询/扫荡协议（2026-10-29，见 game/challenge.py）----
    # 客户端：TiaoZhanPanel ctor 发 5037、ActivityLevelPanel 发 5327/5329、
    # AnimalPanel 发 5034 —— 全是空请求，回包即面板数据源。
    if opcode == OP_THERION_INFO:
        session.send(OP_THERION_INFO, challenge.build_therion_info())
        session.log("GetTherionInfo -> NewTherionInfo（今日圣兽剩余 %d 次）"
                    % challenge.therion_left())
        return
    if opcode == OP_CHALLENGE_INFO:
        session.send(OP_CHALLENGE_INFO, challenge.build_challenge_info())
        session.log("GetChallengeInfo -> 圣兽 %d 次 / 金银岛 %d 次"
                    % (challenge.therion_left(), challenge.coin_left()))
        return
    if opcode in (OP_ACTIVITY_LEVEL_GROUP, OP_ACTIVITY_LEVEL_GROUP_PREVIEW):
        # 打开前先把剩余次数同步进 LEVEL_RECORDS —— 客户端 tryEnterLevel
        # 用 levelData:getLeftTimes() 做入口校验，读的就是这份数据。
        challenge.sync_records()
        _preview = (opcode == OP_ACTIVITY_LEVEL_GROUP_PREVIEW)
        session.send(opcode, challenge.build_activity_level_group(preview=_preview))
        session.log("GetActivityLevelGroup(preview=%s) -> 阵营4组 + 圣兽4岛 + 金银岛"
                    % _preview)
        return
    if opcode == OP_CHALLENGE_SWEEP:
        # VIP 挑战扫荡：IDCountMessage{id=关卡, count=1} -> AwardResult
        _sid, _scnt = req_id_count(tmsg, pb, _read_varint_field)
        _ok_s, _why_s, _pay_s = challenge.sweep(_sid, _scnt)
        session.log("DailyChallengeSweep(levelId=%s) -> %s"
                    % (_sid, "成功" if _ok_s else "拒绝:" + _why_s))
        if not _ok_s:
            session.send(5002, f_int(1, 0) + f_str(2, _why_s))
            return
        _desc_s = _grant_sign_awards(_pay_s["awards"])
        if _pay_s["awards"]:
            _push_new_heroes(session, _pay_s["awards"])
        save_player_data(PLAYER_DATA)
        session.send(OP_UPDATE_CHARACTER, update_character_body())
        from ..proto import ps_protobuf_pb2 as _pb_sw
        _ar = _pb_sw.AwardResult()
        _ar.type = "挑战扫荡"
        for _a in _pay_s["awards"]:
            _sp = _pb_sw.AwardSpecData()
            _sp.type = int(_a["type"])
            _sp.name = ""
            _sp.value = max(1, int(_a["count"]))
            _sp.extra = True
            _ar.datas.add().CopyFrom(_sp)
        session.send(OP_CHALLENGE_SWEEP, _ar.SerializeToString())
        session.log("  扫荡奖励：%s（剩 %d 次）"
                    % ("、".join(_desc_s) or "无", _pay_s["left"]))
        return
    if opcode == OP_READ_MAIL:
        _mid = req_id(tmsg, pb, _read_varint_field)
        ok, reason = mail.mark_read(_mid)
        session.log("ReadMail(id=%s) -> %s" % (_mid, reason))
        if ok:
            save_player_data(PLAYER_DATA)
        session.send(OP_READ_MAIL, f_int(1, _mid))
        return
    if opcode == OP_DELETE_MAIL:
        _mid = req_id(tmsg, pb, _read_varint_field)
        ok, reason = mail.delete(_mid)
        session.log("DeleteMail(id=%s) -> %s" % (_mid, reason))
        if ok:
            save_player_data(PLAYER_DATA)
        session.send(OP_DELETE_MAIL, f_int(1, _mid))
        return
    if opcode == OP_GET_MAIL_AWARD:
        _mid = req_id(tmsg, pb, _read_varint_field)
        ok, reason, awards = mail.claim(_mid)
        session.log("GetMailAward(id=%s) -> %s%s" % (
            _mid, "成功" if ok else "拒绝:" + reason, " 奖励=%s" % awards if ok else ""))
        if ok:
            save_player_data(PLAYER_DATA)
            # 先同步货币/道具，再回 ack（客户端领奖后立即读本地数据刷新）
            session.send(OP_UPDATE_CHARACTER, update_character_body())
            # ★ 必须用 _push_granted_items：新道具要推 401 NewItem（451 对
            #   不存在的条目会被客户端丢弃 → 领取后背包看不见，2026-10-27）
            _push_granted_items(session, awards)
        session.send(OP_GET_MAIL_AWARD, f_int(1, _mid))
        return
    if opcode == OP_GET_ALL_MAIL_AWARD:
        # ★ 客户端不解析回包（只当 ack），但**必须回**，否则 9 秒超时弹断线
        ok, reason, awards, ids = mail.claim_all()
        session.log("GetAllMailAward -> %s%s" % (
            "成功 %d 封" % len(ids) if ok else "拒绝:" + reason,
            " 奖励=%s" % awards if ok else ""))
        if ok:
            save_player_data(PLAYER_DATA)
            session.send(OP_UPDATE_CHARACTER, update_character_body())
            _push_granted_items(session, awards)   # 新道具 401 / 已有 451（同上）
        session.send(OP_GET_ALL_MAIL_AWARD, b"")
        return
    if opcode == OP_CHANGE_HONOR:
        # 换头像/头像框（107）：IDMessage{id} -> IDMessage{id}（同 id 回显，客户端据此刷新）
        _hid = req_id(tmsg, pb, _read_varint_field)
        ok, reason, kind = do_change_honor(_hid)
        session.log("ChangeHonor(id=%s) -> %s%s" % (
            _hid, "成功:" + kind if ok else "拒绝:" + reason, ""))
        if not ok:
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        # ★ 顺序：先推 200 再回 107。
        #   客户端 onChangeHonorHandler 拿到 id 立刻执行 callback 刷新所有格子的"使用中"角标，
        #   而角标读的是 me:head().type —— 200 若排在后面，callback 跑的时候头像还是旧值，
        #   角标就留在旧格子（2026-10-26 用户报障）。
        session.send(OP_UPDATE_CHARACTER, update_character_body())   # characterHead 先落地
        session.send(OP_CHANGE_HONOR, f_int(1, _hid))                 # 再触发列表刷新
        return
    if opcode == OP_CHANGE_NAME:
        # ★ 真正的改名协议（104）：StringMessage{msg=新名字} -> ChangeNameData{result, newName}
        #   客户端 NameChangePanel 的确认按钮发的是这个，不是 105 ChangeAccount（那是换账号）。
        #   result: 1 成功 / 2 重名 / 3 格式错 / 4 改名卡不足 —— 客户端按码弹不同文案。
        _nm = (_read_string_field(pb, 1) or "").strip()
        ok, code, reason = do_change_name(_nm)
        session.log("ChangeName(name=%r) -> %s（code=%s, 改名卡 %s）" % (
            _nm, "成功" if ok else "拒绝:" + reason, code, name_card_count()))
        if not ok:
            session.send(OP_CHANGE_NAME, f_int(1, int(code)) + f_str(2, ""))
            return
        save_player_data(PLAYER_DATA)
        session.send(OP_CHANGE_NAME, f_int(1, NAME_OK) + f_str(2, _nm))
        session.send(OP_UPDATE_CHARACTER, update_character_body())
        session.log("  改名成功 -> %s（改名卡剩 %s 张）" % (_nm, name_card_count()))
        return
    if opcode == OP_CHANGE_ACCOUNT:
        # 改名（105）：ChangeAccountRequest{name, password, age} -> ChangeAccountData{result, newAccount}
        _nm = (_read_string_field(pb, 1) or "").strip()
        session.log("ChangeAccount(name=%r) -> 改名（改名卡 %s 张）" % (_nm, name_card_count()))
        ok, _code, reason = do_change_name(_nm)
        got = char_name() if ok else ""
        if not ok:
            session.log("  拒绝：%s" % reason)
            # result=0 让客户端按失败处理；5002 弹具体原因
            session.send(OP_CHANGE_ACCOUNT, f_int(1, 0) + f_str(2, ""))
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        session.send(OP_CHANGE_ACCOUNT, f_int(1, 1) + f_str(2, got))
        session.send(OP_UPDATE_CHARACTER, update_character_body())
        session.log("  改名成功 -> %s（改名卡剩 %s 张）" % (got, name_card_count()))
        return
    if opcode == OP_SELECT_CHARACTER:
        # IDMessage { required int64 id = 1; }
        session.log("SelectCharacter(id=%s) -> 下发 EnterGame" % req_id(tmsg, pb, _read_varint_field))
        # ★ 主线任务：这是 EnterGame 的**真实入口**（客户端不请求 102，OP_ENTER_GAME 分支是死代码）。
        #   先按历史通关记录把当前节点补标为"可领"，再打包 EnterGame，否则老存档会看到
        #   "让你再打一次 1001" 的僵尸任务（1001 早就通了）。
        try:
            if mission.sync_with_records(LEVEL_RECORDS):
                save_player_data(PLAYER_DATA)
                _m = mission.current() or {}
                session.log("SelectCharacter -> 主线 %s 历史已通关，标记可领" % _m.get("id"))
        except Exception as _e:
            session.log("mission sync 失败: %s" % _e)
        # 月卡/周卡跨天结算：补发每日奖励邮件 + 扣减剩余天数（登录即结算一次）。
        # push=False —— 下面统一推邮箱，避免同一封邮件推两次；记录随 EnterGame 下发。
        try:
            from . import month_card
            _bp_changed = month_card.settle(push=False)
            if _bp_changed:
                save_player_data(PLAYER_DATA)
                for _bp_rec in _bp_changed:
                    session.log("月卡结算: id=%s 剩余 %s 天"
                                % (_bp_rec.get("type"), _bp_rec.get("endDate")))
        except Exception as _e:
            session.log("月卡结算失败: %s" % _e)
        # ★ 邮件：客户端 MailManager:initDataOnLogin **没有任何调用点**，
        #   邮件列表唯一的填充方式是收到 300 NewMail 推送（onSvrNewMail → addSvrMail）。
        #   EnterGame.mails 客户端根本不消费，所以必须在这里逐封推。
        for _mail in mail.to_snapshot():
            try:
                session.send(OP_NEW_MAIL, build_mail_data(_mail))
            except Exception as _e:
                session.log("推邮件失败: %r" % _e)
        _mc = len(mail.to_snapshot())
        if _mc > 0:
            session.log("SelectCharacter -> 已推送 %d 封邮件" % _mc)
        session.send(OP_ENTER_GAME, build_enter_game())
        # 初始化任务推送基线：EnterGame 已带全部任务，之后 push_updates 只推**变化**
        try:
            _ms = mission.snapshot()[0] + mission_achieve.snapshot_missions() \
                + mission_periodic.snapshot_missions()
            session._achieve_pushed = {int(m["id"]): int(m["complete"]) for m in _ms
                                       if int(m["id"]) in mission_achieve._rows()["by_id"]}
            session._periodic_sent = {int(m["id"]): int(m["complete"]) for m in _ms
                                      if int(m["id"]) in {int(r["id"]) for r in mission_periodic._rows()}}
        except Exception as _e:
            flog("[mission] 推送基线初始化失败: %r" % _e)
        # ★ 断线重连回主界面（2026-10-27）：同一次客户端运行内重连（Reconnect>0）
        #   且上一场战斗没结算 → 补发 5001 GameError(18)。客户端在 Battle 场景
        #   收到该错误会弹提示并 replaceScene(GameScene)，与“战斗结算失败”同路径。
        if int(getattr(session, "reconnect", 0) or 0) > 0 and PLAYER_DATA.get("battle_in_progress"):
            session.log("重连时检测到未完成战斗 -> 通知客户端回主界面")
            push_game_error(session, 18, "战斗已中断，已返回主界面")
            _set_battle_in_progress(False)
        # 连续登录推进（M3）：每日首次进入 → 天数+1 / 断签重置
        try:
            from .activities import login_days as _ld
            _days = _ld.on_login()
            session.log("LoginDays 连续登录天数=%s" % _days)
        except Exception as _e:
            session.log("LoginDays on_login 失败: %s" % _e)
        # 在线奖励（M4）：登录时把在线计时基准同步给客户端
        try:
            from .activities import online_award as _oa
            _oa._touch_sync()
        except Exception as _e:
            session.log("online_award sync 失败: %s" % _e)
        # 等级礼包已砍（2026-10-23 用户要求）——不再下发任务、不再处理 10100。
        # 折扣活动（5318）：EnterGame 里不塞 ActivityData（避免触发客户端其它
        # activity 分支），单独推 LotteryActivityInfo 给当前会话。
        push_lottery_activity(session)
        # 从 PLAYER_DATA 初始化关卡通关记录
        # （leftTimes/buyTimes 只在 MaxTimes>0 的关（可扫荡关）才有意义，2026-10-29）
        for lvl_id, rec_data in PLAYER_DATA.get("level_records", {}).items():
            _mt = level_run.max_times(lvl_id)
            _left = level_run.left_times(lvl_id) if _mt > 0 else 0
            _buy = level_run.buy_times(lvl_id) if _mt > 0 else 0
            # 挑战副本（圣兽岛/金银岛/阵营）：leftTimes 是**挑战剩余次数**，
            # 客户端 tryEnterLevel 用它做入口校验（2026-10-29）。
            _cleft = challenge.remaining_for_level(lvl_id)
            if _cleft is not None:
                _left, _buy = _cleft, 0
            rec = b""
            rec += f_int(1, int(lvl_id))
            rec += f_int(2, int(lvl_id))    # type = 关卡id
            rec += f_int(3, rec_data.get("star", 3))
            rec += f_int(4, _left)
            rec += f_int(5, _buy)
            session.send(520, rec)
        session.log("初始化关卡通关记录: %s" % sorted(PLAYER_DATA.get("level_records", {}).keys()))
        # 每日扫荡令牌任务（2026-10-29）：当日未领 → 推 501 供玩家领取
        try:
            daily_token.push_new(session)
        except Exception as e:
            session.log("每日令牌任务推送失败：%r" % e)
    elif opcode == OP_SET_GUIDE_INDEX:
        # SetGuideIndex(IDMessage) 客户端发来后要 ack 同 opCode + id，
        # 否则 GuideManager._commitGuideCallback 不触发，进不了游戏场景
        gid = req_id(tmsg, pb, _read_varint_field) or 1
        # ★ 2026-10-28：以前只 ack 不落库（players.guide_index 恒 0，重启即"引导重来"）。
        #   现在写进 player.guide_index（真实列）并落库；只升不降（客户端偶尔回传旧值）。
        p = PLAYER_DATA.setdefault("player", {})
        if int(p.get("guide_index") or 0) < int(gid):
            p["guide_index"] = int(gid)
            save_player_data(PLAYER_DATA)
        session.log("SetGuideIndex(id=%s) -> ack（存档 guide_index=%s）"
                    % (gid, p.get("guide_index")))
        session.send(OP_SET_GUIDE_INDEX, f_int(1, gid))
    elif opcode in (OP_LEVEL_BEGIN, OP_ACTIVITY_LEVEL_BEGIN):
        # LevelBegin（开始关卡）：按 LevelSetConfig 配置表布置敌方阵容
        # 6007 ActivityLevelBegin（阵营副本）请求体同样是 IDMessage{id=关卡ID}，
        # 回包形状与 5030 完全一致（客户端 onActivityLevelBeginBattleHandler
        # 直接复用 onBeginBattleHandler 解 LevelBegin）。
        lid = req_level_begin(tmsg, pb, _read_varint_field)
        session.log("LevelBegin(levelId=%s, op=%s) -> 按配置表下发 LevelBegin" % (lid, opcode))

        # ---- 挑战副本入口校验（2026-10-29）----
        # 圣兽岛：开岛日 + 每岛每日 1 次；金银岛：每日 2 次；阵营：每组每日 1 次。
        # 不满足只回 5002、不下发 LevelBegin —— 客户端不切场景，玩家看到提示。
        if challenge.is_challenge_level(lid):
            _cok, _cwhy = challenge.can_enter(lid)
            if not _cok:
                session.log("  !! 挑战副本入口拒绝：%s" % _cwhy)
                session.send(5002, f_int(1, 0) + f_str(2, _cwhy))
                return

        # ---- 扣体力（2026-10-29）----
        # 客户端进关前已自检 `me:ep() < getEp()` 并弹买体力框，这里只是服务端兜底。
        # 体力不足时**照常开战**（否则客户端已进战斗场景、拿不到 LevelBegin 会卡死），
        # 但不扣值，并推 5002 让玩家知道这次没扣成功（避免"0 体力无限刷"）。
        _cost_ep = level_run.ep_cost(lid)
        if _cost_ep > 0:
            _ok_ep, _ep_reason = economy.spend(_cost_ep, PLAYER_DATA)
            if _ok_ep:
                session.log("  扣体力 %d（剩 %d/%d）" % (
                    _cost_ep, economy.sync(PLAYER_DATA), economy.ep_max(PLAYER_DATA)))
                session.send(OP_UPDATE_CHARACTER, update_character_body())
                save_player_data(PLAYER_DATA)
            else:
                session.log("  !! 体力不足，未扣：%s" % _ep_reason)
                session.send(5002, f_int(1, 0) + f_str(2, _ep_reason))

        # 从 LevelSetConfig 读敌人配置
        enemies_cfg = game_config.get_level_enemies(lid)
        session.log("  LevelSetConfig[%s] -> %d 个敌人" % (lid, len(enemies_cfg)))

        # 按 Wave 分组建 BattleGroupData
        waves = {}
        for ec in enemies_cfg:
            w = ec["wave"]
            if w not in waves:
                waves[w] = []
            waves[w].append(ec)

        body = b""
        body += f_int(1, lid)            # required id

        eid_counter = 2000
        for wave_num in sorted(waves.keys()):
            wave_enemies = waves[wave_num]
            heros = []
            for ec in wave_enemies:
                eid_counter += 1
                h = build_enemy_hero_data(
                    eid_counter, ec["type"], ec["position"],
                    stats={"hp": ec["hp"], "atk": ec["attack"], "def": ec["defence"],
                           "crit": ec["crit"], "combo": ec["combo"], "block": ec["block"],
                           "counter": ec["counter"], "crit_damage": ec["crit_damage"],
                           "tenacity": ec.get("tenacity", 0),
                           "hit": ec.get("hit", 0), "miss": ec.get("miss", 0),
                           "damage_increase": ec.get("damage_increase", 0),
                           "damage_decrease": ec.get("damage_decrease", 0),
                           "is_devil": ec.get("is_devil", 0)},
                    scale=ec.get("scale", 100),
                    grade=ec.get("grade_level", 0),
                    lv=ec.get("show_level", 1),
                )
                heros.append(h)
                session.log("  敌人: type=%s pos=%s hp=%s(x%s%%) atk=%s def=%s 档=%s" % (
                    ec["type"], ec["position"],
                    int(round(ec["hp"] * ec.get("scale", 100) / 100.0)), ec.get("scale", 100),
                    int(round(ec["attack"] * ec.get("scale", 100) / 100.0)),
                    int(round(ec["defence"] * ec.get("scale", 100) / 100.0)),
                    ec.get("grade_level", 0)))
            group = build_battle_group_data(wave_num, 1, heros)
            body += f_msg(3, group)     # repeated groups (BattleGroupData)

        # 我方阵容：全部 position>0 的武将
        my_group = b""
        on_queue = [(h["id"], h["type"], h.get("lv", 1), h.get("position", -1))
                    for h in PLAYER_DATA.get("heroes", {}).values()
                    if h.get("position", -1) > 0]
        on_queue.sort(key=lambda x: x[3])
        # 军旗加成（2026-10-24）：现在由 builders_pb.hero_msg 内部按
        # `h.position>0` 自动叠加（上阵武将才吃），这里只打日志确认。
        _flag_bon = ensign.squad_flag_bonuses(0, 1)
        if _flag_bon:
            session.log("  军旗加成 %d 条词条: %s" % (len(_flag_bon), _flag_bon))
        for hid, htype, hlv, hpos in on_queue[:5]:
            my_group += f_msg(1, build_hero_data(hid, htype, hlv, hpos))
        my_group += f_int(2, team_power())   # power（team_power 默认已含军旗加成）
        # 军旗（2026-10-23）：HeroGroupData.flagParts = 上阵军旗的部件 type 列表
        # （客户端 BattleScenePVE 用它 battleInit 生成 EnsignBearer + 套装技能）
        for _pt in ensign.squad_flag_part_types(0, 1):
            my_group += f_int(3, _pt)
        body += f_msg(4, my_group)       # optional myGroup (HeroGroupData)
        # ★ 回包必须挂在**请求**的 opcode 上：6007 ActivityLevelBegin 的 ack
        #   注册在 6007（ActivityLevelBeginRt），挂 5030 客户端会直接丢弃。
        session.send(opcode, body)

    elif opcode in (OP_LEVEL_END, OP_ACTIVITY_LEVEL_END):
        # LevelEnd（战斗结算）：客户端发 LevelBattleRecord{id, result, star, ...}
        # 服务器回 LevelResult{id, teamExp, coin, exp, props}
        # 6008 ActivityLevelEnd（阵营副本）同形状，客户端 onActivityLevelResultHandler
        # 直接复用 onLevelResultHandler(msg, true) 解 LevelResult。
        lid, result_val, star = req_level_end(tmsg, pb, _read_varint_field)
        session.log("LevelEnd(levelId=%s, result=%s, star=%s, op=%s) -> 下发 LevelResult"
                    % (lid, result_val, star, opcode))

        # ---- 挑战副本结算分支（圣兽岛/金银岛/阵营副本，2026-10-29）----
        # 奖励与主线不同（升星材料 / 伤害换铜钱 / 随机武将），且要回填挑战次数，
        # 所以单独一条路径，不掺进主线关卡的逻辑。
        if challenge.is_challenge_level(lid):
            _challenge_level_end(session, opcode, lid, result_val, star, tmsg)
            return

        # ---- 战斗结果合理性校验（不重跑模拟，规则见 battle_validate）----
        _br = getattr(tmsg, "battleRecord", None) if tmsg is not None else None
        _mg = getattr(_br, "myGroup", None) if _br is not None else None
        _rep_ids = [h.id for h in (_mg.heros if _mg is not None else [])]
        _owner = {int(h.get("id") or 0) for h in PLAYER_DATA.get("heroes", {}).values()}
        # 服务端同阵容战力（与上报 power 对比）
        _sp = 0
        for _h in PLAYER_DATA.get("heroes", {}).values():
            if int(_h.get("id") or 0) in set(_rep_ids):
                _st = hero_attrs(_h.get("type"), _h.get("lv", 1), _h.get("grade", 0))
                # 与 team_power 同口径：缘分+装备+军旗（上报 power 是客户端全口径）
                apply_fate_to_stats(_st, _h, ensign.squad_flag_bonuses_for_hero(_h.get("type")))
                _sp += int(_st.get("power") or 0)
        _ok, _rule, _reason = battle_validate.validate_level_end(game_config, {
            "lid": lid,
            "result": result_val,
            "star": star,
            "total_damage": getattr(tmsg, "totalDamage", None) if tmsg is not None else None,
            "use_time": getattr(tmsg, "useTime", None) if tmsg is not None else None,
            "my_die_count": getattr(_br, "myDieCout", None) if _br is not None else None,
            "my_group_ids": _rep_ids,
            "my_group_power": getattr(_mg, "power", None) if _mg is not None else None,
            "server_power": _sp,
            "owner_hero_ids": _owner,
        })
        if not _ok:
            session.log("LevelEnd 校验拒绝[%s]: %s (lid=%s result=%s star=%s)" % (
                _rule, _reason, lid, result_val, star))
            session.send(5002, f_int(1, 0) + f_str(2, "战斗结果未通过校验：%s" % _reason))
            # 按失败处理：零奖励 LevelResult，不发进度、不加经验
            _z = b""
            _z += f_int(1, lid)
            _z += f_int(2, 0)   # teamExp
            _z += f_int(3, 0)   # coin
            _z += f_int(4, 0)   # exp
            session.send(OP_LEVEL_END, _z)
            return
        session.log("LevelEnd 校验通过[%s]: %s" % (_rule, _reason))

        # 战报：把客户端上传的 BattleRecordData 存下来，battleID 随 LevelResult 下发。
        # 客户端「战报」按钮就是拿这个 id 发 ShowBattle(5070) 取回整份记录。
        _battle_id = remember_battle_record(
            lid, _br, result_val, star,
            getattr(tmsg, "useTime", None) if tmsg is not None else None)

        # 胜利才记进度（服务器持久语义；重登时 EnterGame 会带上）
        if result_val >= 1:
            _old = LEVEL_RECORDS.get(lid) or LEVEL_RECORDS.get(str(lid)) or {}
            _old_star = int(_old.get("star") or 0)
            # 星级由**服务端**定（客户端只上报 0/1，见 battle_validate.star_domain）：
            # 按阵亡人数评星 —— 0 死 3 星 / 1 死 2 星 / 其余 1 星。
            _die = getattr(_br, "myDieCout", None) if _br is not None else None
            try:
                _die = int(_die)
            except (TypeError, ValueError):
                _die = None
            if _die is None:
                _srv_star = 1
            elif _die == 0:
                _srv_star = 3
            elif _die == 1:
                _srv_star = 2
            else:
                _srv_star = 1
            _srv_star = max(_srv_star, _old_star)     # 只升不降
            # ★ 键必须是 str：LEVEL_RECORDS 里混用 int/str 键时，repo 会把同一条
            #   关卡写成两行 → level_records 唯一索引冲突 → **所有落库全部失败**
            #   （2026-10-29 用户实测：领完的礼包/扫荡进度重启后回滚）。
            LEVEL_RECORDS[str(lid)] = {
                "star": _srv_star,
                "leftTimes": level_run.left_times(lid),
                "buyTimes": level_run.buy_times(lid),
            }

            # ---- 免费购买次数（2026-10-24）----
            # ① 通关「本章最后一关」→ 免费买体力 +1（只算首次通关）
            # ② 通关「BOSS 关」且拿到 3 星 → 免费买铜钱 +1（只在首次上三星时给）
            _lc = (game_config.load_level_config() or {}).get(lid) or \
                  (game_config.load_level_config() or {}).get(str(lid)) or {}
            if int(_lc.get("IsFinalLevel") or 0) == 1 and _old_star == 0:
                _fe = buy.add_free_ep(1)
                session.log("  章末关 %s 首通 → 免费买体力 +1（现 %s 次）" % (lid, _fe))
            if int(_lc.get("IsBoss") or 0) == 1 and _srv_star >= 3 and _old_star < 3:
                _fc = buy.add_free_coin(1)
                session.log("  BOSS 关 %s 三星 → 免费买铜钱 +1（现 %s 次）" % (lid, _fc))

            # ---- 成就计数：万人斩(击杀)/千军破(BOSS)，2026-10-29 ----
            try:
                mission_achieve.bump_counter("kill", len(game_config.get_level_enemies(lid) or []))
                if int(_lc.get("IsBoss") or 0) >= 2:   # 带难度的 BOSS 关（1=章内小BOSS，不算）
                    mission_achieve.bump_counter("boss")
                mission_achieve.push_updates(session)
                mission_periodic.push_refresh(session)
                
            except Exception as _e:
                flog("[mission] 击杀/BOSS 计数失败: %r" % _e)

            # ---- 主线任务：通关指定关卡即达成（Target=7, TargetValue=1）----
            _mchanged, _mnode = mission.on_level_win(lid)
            if _mchanged:
                session.send(OP_UPDATE_MISSION, build_mission_data({
                    "id": _mnode["id"], "type": _mnode["id"],
                    "need": int(_mnode.get("target_value") or 1),
                    "complete": 1,
                    "accept": int(mission.state().get("accept") or 0),
                }))
                session.log("  主线任务 %s（关卡%s）达成 → 502" % (
                    _mnode["id"], _mnode.get("target_id")))
            save_player_data(PLAYER_DATA)   # ★ 推关进度持久化

        body = b""
        body += f_int(1, lid)           # required id
        body += f_int(2, 100)           # teamExp（主公/队伍经验，进度条用）
        body += f_int(3, level_run.LEVEL_COIN)   # coin
        # exp：每个上阵武将本关获得的经验（固定 80，见 growth.STAGE_HERO_EXP）
        hero_exp = growth.team_exp()
        body += f_int(4, hero_exp)      # exp
        if _battle_id:
            body += f_str(7, _battle_id)   # battleID：客户端「战报」按钮的查询 key

        # ---- 队伍经验 + 概率额外掉落（2026-10-29）----
        # teamExp 之前只下发不结算 → 队伍经验条永远不动；现在真加（可能升级，升级补体力）。
        # 掉落按 config/level_drop.json 的概率从 LevelConfig.ShowAward 池里抽。
        drops = []
        if result_val >= 1:
            _otl, _ote, _ntl, _nte = level_run.add_team_exp(100)
            if _ntl != _otl:
                session.log("  队伍升级 %d -> %d（补体力 %d）" % (
                    _otl, _ntl, economy.ep_max(PLAYER_DATA)))
                # 升级 → 等级礼包（-1）可能解锁新档
                try:
                    mission_achieve.push_updates(session)
                    mission_periodic.push_refresh(session)
                except Exception as _e:
                    flog("[mission] 等级礼包刷新失败: %r" % _e)
            # 通关 → 星星收集狂（13）进度变化
            try:
                mission_achieve.push_updates(session)
                mission_periodic.push_refresh(session)
            except Exception as _e:
                flog("[mission] 星星任务刷新失败: %r" % _e)
            drops = level_run.roll_drops(lid, 1)
            if drops:
                _desc = _grant_sign_awards(drops)
                _push_granted_items(session, drops)
                _push_new_heroes(session, drops)
                session.log("  额外掉落：%s" % "、".join(_desc))
                for _a in drops:
                    body += f_msg(6, _award_spec_bytes(
                        _a.get("type"), _a.get("count"),
                        extra=(_a.get("kind") in ("hero", "equip"))))

        # expChanges: repeated ExpChange{id, oldExp, oldLv, newExp, newLv, addExp}
        # 真正把经验加到每个上阵武将身上；满级的武将 addExp 记 0
        changes = []
        if result_val >= 1:
            on_field = sorted(
                [h for h in PLAYER_DATA.get("heroes", {}).values()
                 if h.get("position", 0) > 0],
                key=lambda x: x.get("position", 0))[:5]
            for h in on_field:
                row = growth.find_hero_row(h.get("type"))
                if not row:
                    continue
                q = int(growth._num(row.get("Quality")))
                cap = growth.level_cap(q, h.get("awaken", 0), h.get("can_break"))
                old_lv, old_exp = int(h.get("lv") or 1), int(h.get("exp") or 0)
                nl, ne, ups, wasted = growth.add_exp(
                    q, old_lv, old_exp, hero_exp, cap, h.get("can_break"))
                h["lv"], h["exp"] = nl, ne
                gained = 0 if wasted >= hero_exp else hero_exp
                changes.append((h["id"], old_exp, old_lv, ne, nl, gained))
                if ups:
                    session.log("  武将#%s(%s) +%d经验 %d级 -> %d级" % (
                        h["id"], game_config.get_hero_name(h.get("type")),
                        gained, old_lv, nl))
            if changes:
                save_player_data(PLAYER_DATA)
                session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
                session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
        for cid, oexp, olv, nexp, nlv, add in changes:
            exp_chg = b""
            exp_chg += f_int(1, cid)        # id (hero id)
            exp_chg += f_int(2, oexp)       # oldExp
            exp_chg += f_int(3, olv)        # oldLv
            exp_chg += f_int(4, nexp)       # newExp
            exp_chg += f_int(5, nlv)        # newLv
            exp_chg += f_int(6, add)        # addExp
            body += f_msg(5, exp_chg)       # repeated expChanges
        session.send(OP_LEVEL_END, body)

        # 追发 LevelRecordData（更新关卡星级/通关状态，解锁下一关）
        rec = b""
        rec += f_int(1, lid)            # required id
        rec += f_int(2, lid)            # type（=关卡id，updateLevelData 用这个查）
        rec += f_int(3, star)           # star
        rec += f_int(4, level_run.left_times(lid))   # leftTimes（今日剩余扫荡次数）
        rec += f_int(5, level_run.buy_times(lid))    # buyTimes（今日已重置次数）
        session.send(520, rec)             # NewLevelRecord=520
        session.log("LevelRecordData(type=%s, star=%s) -> 解锁状态更新" % (lid, star))

        # 推一次武将最新数据，让主界面的等级/经验跟着变（结算面板只读 expChanges）
        for cid, oexp, olv, nexp, nlv, add in changes:
            if nlv != olv or nexp != oexp:
                hh = _find_hero(cid)
                if hh:
                    session.send(OP_UPDATE_HERO, build_hero_data(
                        cid, hh["type"], hh.get("lv", 1), hh.get("position", 0)))

        # 持久化到 PLAYER_DATA（leftTimes/buyTimes 走每日状态，2026-10-29）
        _left = level_run.set_left(lid, level_run.left_times(lid))
        PLAYER_DATA.setdefault("level_records", {})[str(lid)] = {
            "star": star, "leftTimes": _left,
            "buyTimes": level_run.buy_times(lid),
        }
        save_player_data(PLAYER_DATA)
        session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
        session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
        session.log("通关记录已保存到 player_data.json")
    elif opcode == OP_SWEEP:
        # 扫荡（2026-10-29）：IDCountMessage{id=关卡, count=次数} -> LevelResults
        # 客户端 MopUpPanel → AwardContainer 只读 results[1] 的 coin/props，
        # 次数由面板自己显示（_mopupTimes），所以这里把 N 次**合并成一条**下发，
        # 免得玩家只看到 1/N 的奖励。
        _lid, _count = req_id_count(tmsg, pb, _read_varint_field)
        _count = max(1, min(10, int(_count or 1)))
        session.log("ReapLevel(levelId=%s, count=%s) -> 扫荡" % (_lid, _count))
        _ok, _reason, _pay = level_run.do_sweep(_lid, _count)
        if not _ok:
            session.log("  扫荡被拒：%s" % _reason)
            session.send(5002, f_int(1, 0) + f_str(2, _reason))
            return

        _rw = _pay["rewards"]
        # 铜币
        _p = PLAYER_DATA.setdefault("player", {})
        _p["coin"] = int(_p.get("coin") or 0) + int(_rw["coin"])
        # ---- 成就刷新：扫荡计数在 level_run.do_sweep 内完成，这里推 501/502 ----
        try:
            mission_achieve.push_updates(session)
            mission_periodic.push_refresh(session)
        except Exception as _e:
            flog("[mission] 扫荡刷新失败: %r" % _e)
        # 额外掉落（授予 + 推送 401/403/404）
        if _rw["props"]:
            _desc = _grant_sign_awards(_rw["props"])
            _push_granted_items(session, _rw["props"])
            _push_new_heroes(session, _rw["props"])
            session.log("  扫荡掉落：%s" % "、".join(_desc))

        # ---- LevelResults ----
        _lr = b""
        _lr += f_int(1, _lid)                       # required id
        _lr += f_int(2, 100 * _count)               # teamExp
        _lr += f_int(3, int(_rw["coin"]))           # coin
        _lr += f_int(4, int(_rw["exp"]))            # 每个上阵武将经验
        for _a in (_rw["props"] or []):
            _lr += f_msg(6, _award_spec_bytes(
                _a.get("type"), _a.get("count"),
                extra=(_a.get("kind") in ("hero", "equip"))))
        for cid, oexp, olv, nexp, nlv, add in (_pay["exp_changes"] or []):
            _ec = b""
            _ec += f_int(1, cid)
            _ec += f_int(2, oexp)
            _ec += f_int(3, olv)
            _ec += f_int(4, nexp)
            _ec += f_int(5, nlv)
            _ec += f_int(6, add)
            _lr += f_msg(5, _ec)

        _body = f_msg(1, _lr)
        _otl, _ote, _ntl, _nte = _pay["character_change"]
        if _ntl != _otl:
            try:
                mission_achieve.push_updates(session)
                mission_periodic.push_refresh(session)
            except Exception as _e:
                flog("[mission] 成就刷新失败: %r" % _e)
        _cc = b""
        _cc += f_int(1, CHAR_ID)
        _cc += f_int(2, int(_ote))
        _cc += f_int(3, int(_otl))
        _cc += f_int(4, int(_nte))
        _cc += f_int(5, int(_ntl))
        _body += f_msg(3, _cc)
        # ★ 先推 520/451，再回 5032：客户端 MopUpPanel:showAwards 一收到扫荡回包就
        #   读 `getLevelData():getLeftTimes()` 刷新剩余次数/按钮，520 若后到，
        #   面板这一帧读到的还是旧次数（表现为"扫荡完还显示 5 次"）。
        _rec = b""
        _rec += f_int(1, _lid)
        _rec += f_int(2, _lid)
        _rec += f_int(3, level_run.star_of(_lid))
        _rec += f_int(4, level_run.left_times(_lid))
        _rec += f_int(5, level_run.buy_times(_lid))
        session.send(520, _rec)
        # 扣了扫荡令牌 → 451 更新数量（用光时 push_item_count 会改推 499 删条目）。
        # ★ 必须在 5032 之前推：客户端 MopUpPanel:showAwards 收到回包立刻读
        #   bagManager 刷新「扫荡令牌 xN」，451 若后到，标签就停在上一次的值。
        try:
            push_item_count(session, level_run.SIDE_TOKEN_ITEM)
        except Exception as _e:
            session.log("  令牌数量推送失败：%r" % _e)

        session.send(OP_SWEEP, _body)
        session.log("  扫荡 x%d 完成：铜币+%d 队伍经验 %d->%d（%d/%d）"
                    % (_pay["count"], _rw["coin"], _otl, _ntl,
                       int(_nte), int((game_config.load_exp_set_config()
                                       .get(_ntl) or {}).get("TeamExp") or 0)))

        for cid, oexp, olv, nexp, nlv, add in (_pay["exp_changes"] or []):
            _hh = _find_hero(cid)
            if _hh:
                session.send(OP_UPDATE_HERO, build_hero_data(
                    cid, _hh["type"], _hh.get("lv", 1), _hh.get("position", 0)))
        save_player_data(PLAYER_DATA)
        session.send(OP_UPDATE_CHARACTER, update_character_body())
        if _ntl != _otl:
            session.send(OP_SQUAD_POWER, build_squad_power_change())
    elif opcode == OP_BUY_LEVEL_TIMES:
        # 重置关卡扫荡次数（2026-10-29）：IDMessage{id=关卡} -> IDCountMessage{id,count}
        _lid = req_id(tmsg, pb, _read_varint_field)
        _ok, _reason, _left = level_run.do_buy_times(_lid)
        if not _ok:
            session.log("BuyLevelTimes(%s) 拒绝：%s" % (_lid, _reason))
            session.send(5002, f_int(1, 0) + f_str(2, _reason))
            # 客户端在等 IDCountMessage，回一下当前次数避免面板卡住
            session.send(OP_BUY_LEVEL_TIMES,
                         f_int(1, _lid) + f_int(2, level_run.left_times(_lid)))
            return
        session.log("BuyLevelTimes(%s) -> 重置成功，剩余 %d 次（第 %d 次重置）"
                    % (_lid, _left, level_run.buy_times(_lid)))
        session.send(OP_BUY_LEVEL_TIMES, f_int(1, _lid) + f_int(2, _left))
        session.send(OP_UPDATE_CHARACTER, update_character_body())   # 元宝变化
        _rec = b""
        _rec += f_int(1, _lid)
        _rec += f_int(2, _lid)
        _rec += f_int(3, level_run.star_of(_lid))
        _rec += f_int(4, _left)
        _rec += f_int(5, level_run.buy_times(_lid))
        session.send(520, _rec)
        save_player_data(PLAYER_DATA)
    elif opcode == OP_SHOW_BATTLE:
        # 战报（2026-10-29）：ShowBattle{key=battleID} → AttackArena
        # ★ 回包类型必须是 AttackArena（客户端 showBattleVideo 里 decode("AttackArena")），
        #   不是 BattleRecordData，也不是 LevelResult —— 回错类型面板必然空。
        _key = getattr(tmsg, "key", None) if tmsg is not None else None
        if _key in (None, ""):
            try:
                _key = _read_string_field(pb, 1)
            except Exception:
                _key = ""
        _rec = get_battle_record(_key)
        if _rec is None:
            session.log("ShowBattle(key=%r) -> 无战报记录（可能服务端重启过）" % (_key,))
            session.send(OP_SHOW_BATTLE, f_int(1, 0) + f_int(2, 0))
            return
        _body = b""
        _body += f_int(1, _rec["lid"])      # required id（客户端不用于查询，填关卡 id）
        _body += f_int(2, _rec["result"])   # result：1=胜（BattleRecordPanel 用它判胜负）
        _body += f_msg(5, _rec["raw"])      # battleRecord：LevelEnd 上传的原样字节
        _body += f_str(9, _rec["key"])      # recordKey（回退命中时也对得上）
        session.send(OP_SHOW_BATTLE, _body)
        session.log("ShowBattle(key=%s) -> AttackArena{id=%s result=%s record=%d bytes}"
                    % (_key, _rec["lid"], _rec["result"], len(_rec["raw"])))
    elif opcode == OP_ENTER_GAME:
        # 主线任务：按历史通关记录把当前节点补标为"已达成"（否则老存档会看到
        # "再打一次 1001" 的僵尸任务）
        if mission.sync_with_records(LEVEL_RECORDS):
            save_player_data(PLAYER_DATA)
            session.log("EnterGame -> 主线任务 %s 历史已通关，标记可领" % (
                (mission.current() or {}).get("id")))
        session.log("EnterGame -> levelRecords=%s" % sorted(LEVEL_RECORDS.keys()))
        session.send(OP_ENTER_GAME, build_enter_game())
        # 初始化任务推送基线：EnterGame 已带全部任务，之后 push_updates 只推**变化**
        try:
            _ms = mission.snapshot()[0] + mission_achieve.snapshot_missions() \
                + mission_periodic.snapshot_missions()
            session._achieve_pushed = {int(m["id"]): int(m["complete"]) for m in _ms
                                       if int(m["id"]) in mission_achieve._rows()["by_id"]}
            session._periodic_sent = {int(m["id"]): int(m["complete"]) for m in _ms
                                      if int(m["id"]) in {int(r["id"]) for r in mission_periodic._rows()}}
        except Exception as _e:
            flog("[mission] 推送基线初始化失败: %r" % _e)
    elif opcode == OP_EQUIP_HERO:
        # 上阵/调整位置：EquipHeroToBattleGameplayIndexRequest{category, index, id, position, categoryPlayMode}
        # 回 BattleGameplayIndexHero{category, index, hero: BattleHeroData{id, position}}
        category, index, hero_id, position = req_equip_hero(tmsg, pb, _read_varint_field)
        session.log("EquipHeroToSquad(category=%s, index=%s, heroId=%s, pos=%s) -> 回 BattleGameplayIndexHero" % (
            category, index, hero_id, position))
        displaced = do_change_hero_position(hero_id, position)
        save_player_data(PLAYER_DATA)
        session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
        session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
        # ★ 上阵武将吃了编队军旗加成 → 必须重推他的 HeroData，否则武将详情战力不变
        push_hero_attr(hero_id)
        if displaced is not None:
            push_hero_attr(displaced["id"])   # 被顶下去的，不再吃加成
        # 回 BattleGameplayIndexHero（ack，含缘分提示流程）
        hero_data = build_battle_hero_data(hero_id, position)
        resp = b""
        resp += f_int(1, category)
        resp += f_int(2, index)
        resp += f_msg(3, hero_data)   # hero (BattleHeroData)
        session.send(OP_EQUIP_HERO, resp)
        # ★ 换位时补发批量同步（UpdateHerosInBattleGameplaySquad=20414）：
        #   一帧带 A+B 两个 BattleHeroData，客户端 onUpdateHerosInSquadHandler
        #   按 id 全量更新位置（否则 B 的本地位置不刷新 → 显示空但实际仍上阵）
        if displaced is not None:
            bh = b""
            bh += f_int(1, category)
            bh += f_int(2, index)
            bh += f_msg(3, build_battle_hero_data(hero_id, position))
            bh += f_msg(3, build_battle_hero_data(displaced["id"], displaced["position"]))
            session.send(20414, bh)
        # ★ 2026-10-28 修「明明 3 人上阵却提示只能上阵 5 个」：
        #   客户端 checkEquipHero 用 table.nums 数 squad 列表**全部条目**，
        #   而被顶下阵的武将以 position=0 留在列表里（onUpdateHerosInSquadHandler
        #   只改位置/插入、从不删条目）→ 幽灵条目攒到 5 条后永远误报上限。
        #   补一帧 20410（UpdateBattleGameplaySquadRt，onUpdateSquadHandler
        #   是 **整表替换** squad.heros、无副作用），全量重推当前编队，
        #   幽灵条目自愈清除。handlerState=0 不弹任何提示框。
        _sq = [(h["id"], h["position"]) for h in PLAYER_DATA.get("heroes", {}).values()
               if h.get("position", -1) > 0]
        _sq.sort(key=lambda x: x[1])
        session.send(20410, build_update_squad_flag_data(
            category, 0, index, _sq[:5], team_power(),
            ensign.get_squad_flag(category, index)))
    elif opcode == OP_UNEQUIP_HERO:
        # 下阵：IDBattleGameplayIndex{category, index, heroIds=[id]}
        # 回 IDBattleGameplayIndex{category, index, heroIds, flagID}
        category, index, hero_id, _pos = req_equip_hero(tmsg, pb, _read_varint_field)
        session.log("UnequipHeroFromSquad(category=%s, index=%s, heroId=%s) -> 回 IDBattleGameplayIndex" % (
            category, index, hero_id))
        # 更新 PLAYER_DATA
        for h in PLAYER_DATA.get("heroes", {}).values():
            if h["id"] == hero_id:
                h["position"] = 0
        save_player_data(PLAYER_DATA)
        session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
        session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
        push_hero_attr(hero_id)   # ★ 下阵后不再吃军旗加成，重推属性
        # 回 IDBattleGameplayIndex
        resp = b""
        resp += f_int(1, category)
        resp += f_int(2, index)
        resp += f_int(3, hero_id)     # repeated heroIds (只发一个)
        resp += f_int(4, ensign.get_squad_flag(category, index))   # flagID（军旗系统）
        session.send(OP_UNEQUIP_HERO, resp)
        # ★ 2026-10-28：下阵后同样全量重推 20410（整表替换），自愈历史幽灵条目
        _sq = [(h["id"], h["position"]) for h in PLAYER_DATA.get("heroes", {}).values()
               if h.get("position", -1) > 0]
        _sq.sort(key=lambda x: x[1])
        session.send(20410, build_update_squad_flag_data(
            category, 0, index, _sq[:5], team_power(),
            ensign.get_squad_flag(category, index)))
    elif opcode == OP_FLAG_PART_PRODUCT:
        # 制作部件（5346）：MultIDCounteListByIDMessage{id=部件type, idCount=[...]}
        # 客户端传的 idCount 是它折算好的背包实例分配——服务端按配置 ProductCost
        # 自主扣料（Replacement 4:1 自动向下折算），451/200 同步修正显示。
        tmsg2 = tmsg if tmsg is not None else None
        part_type = int(tmsg2.id) if tmsg2 is not None else int(_read_varint_field(pb, 1) or 0)
        # ★ 客户端 `ConvenientForgingPanel` 把「材料各品质 + 材料箱各品质」的扣料分配
        #   算好放在 idCount 里，界面显示的消耗就是这份清单。服务端照单扣才能
        #   「消耗与界面一致」（否则服务端自主折算会多烧高品质材料箱）。
        plan = []
        if tmsg2 is not None:
            try:
                for grp in tmsg2.idCount:
                    for ic in grp.idCount:
                        if int(ic.id) > 0 and int(ic.count) > 0:
                            plan.append((int(ic.id), int(ic.count)))
            except Exception as _e:
                session.log("  解析扣料清单失败（回退自主折算）: %r" % _e)
                plan = []
        session.log("FlagPartProduct(type=%s) 扣料清单=%s -> 制作部件" % (
            part_type, plan or "（客户端未提供，自主折算）"))
        ok, reason, part, deltas, coin = ensign.make_part(part_type, plan=plan or None)
        if not ok:
            session.log("  拒绝：%s" % reason)
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        _push_item_deltas(session, deltas)
        if coin:
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        session.send(OP_FLAG_PART_PRODUCT, build_flag_part_data(part))
        session.log("  部件 %s(type=%s) 词条=%s" % (
            part["id"], part["type"], [(a["type"], a["value"], a["maxValue"]) for a in part["additions"]]))
    elif opcode == OP_FLAG_PART_UP:
        # 升级部件（5347）：MultIDCounteListByIDMessage{id=部件实例id} -> FlagPartData
        part_id = int(tmsg.id) if tmsg is not None else int(_read_varint_field(pb, 1) or 0)
        plan = []
        if tmsg is not None:
            try:
                for grp in tmsg.idCount:
                    for ic in grp.idCount:
                        if int(ic.id) > 0 and int(ic.count) > 0:
                            plan.append((int(ic.id), int(ic.count)))
            except Exception:
                plan = []
        session.log("FlagPartUp(id=%s) 扣料清单=%s -> 升级部件" % (
            part_id, plan or "（自主折算）"))
        ok, reason, part, deltas, coin = ensign.level_up_part(part_id, plan=plan or None)
        if not ok:
            session.log("  拒绝：%s" % reason)
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        _push_item_deltas(session, deltas)
        if coin:
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        session.send(OP_FLAG_PART_UP, build_flag_part_data(part))
        push_flag_bonus_change()
    elif opcode == OP_FLAG_PART_UNLOCK_ADD:
        # 解锁词条（5348）：IDMessage -> FlagPartData（当前规则制作即全解锁，正常不触发）
        part_id = req_id(tmsg, pb, _read_varint_field)
        ok, reason, part, deltas, coin = ensign.unlock_addition(part_id)
        if not ok:
            session.log("FlagPartUnlockAdd(%s) 拒绝：%s" % (part_id, reason))
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        _push_item_deltas(session, deltas)
        if coin:
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        session.send(OP_FLAG_PART_UNLOCK_ADD, build_flag_part_data(part))
        push_flag_bonus_change()
    elif opcode == OP_FLAG_PART_RESOLVE:
        # 分解部件（5349）：IDMessage -> IDListMessage{id=[被删部件]}
        part_id = req_id(tmsg, pb, _read_varint_field)
        ok, reason, rid, deltas, coin = ensign.resolve_part(part_id)
        if not ok:
            session.log("FlagPartResolve(%s) 拒绝：%s" % (part_id, reason))
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        _push_item_deltas(session, deltas)      # 先同步返还材料
        if coin:
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        session.send(OP_FLAG_PART_RESOLVE, f_int(1, rid))   # IDListMessage{id=[部件id]}（最后 ack）
        push_flag_bonus_change()
        session.log("FlagPartResolve(%s) 返还 材料=%s 铜币=%s" % (rid, deltas, coin))
    elif opcode == OP_EQUIP_FLAG_PART:
        # 装备部件到旗帐（5351）：PropActionMessage{id=部件, targetID=旗帐} -> FlagPartData
        part_id, flag_id = req_prop_action(tmsg, pb, _read_varint_field)
        ok, reason, part = ensign.equip_part(part_id, flag_id)
        if not ok:
            session.log("EquipFlagPart(%s->%s) 拒绝：%s" % (part_id, flag_id, reason))
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        session.send(OP_EQUIP_FLAG_PART, build_flag_part_data(part))
        push_flag_bonus_change()
        session.log("EquipFlagPart(部件%s -> 旗帐%s) 成功" % (part_id, flag_id))
    elif opcode == OP_UNEQUIP_FLAG_PART:
        # 卸下部件（5352）：IDMessage -> FlagPartData（host=0）
        part_id = req_id(tmsg, pb, _read_varint_field)
        ok, reason, part = ensign.unequip_part(part_id)
        if not ok:
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        session.send(OP_UNEQUIP_FLAG_PART, build_flag_part_data(part))
        push_flag_bonus_change()
    elif opcode == OP_EQUIP_FLAG:
        # 军旗上阵（5353）：EquipFlagToBattleGameplayIndexRequest{category,index,id,categoryPlayMode}
        # -> UpdateBattleGameplaySquadDatas{gameplayDataSquadFlags=[...]}
        if tmsg is not None:
            category, index, flag_id, cpm = (int(tmsg.category), int(tmsg.index),
                                             int(tmsg.id), int(tmsg.categoryPlayMode))
        else:
            category = int(_read_varint_field(pb, 1) or 0)
            index = int(_read_varint_field(pb, 2) or 0)
            flag_id = int(_read_varint_field(pb, 3) or 0)
            cpm = int(_read_varint_field(pb, 4) or 0)
        ok, reason = ensign.set_squad_flag_checked(category, index, flag_id)
        if not ok:
            session.log("EquipFlag(%s->%s/%s) 拒绝：%s" % (flag_id, category, index, reason))
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        heros = [(h["id"], h["position"]) for h in PLAYER_DATA.get("heroes", {}).values()
                 if h.get("position", -1) > 0]
        heros.sort(key=lambda x: x[1])
        body = build_update_squad_flag_datas([
            (category, cpm, index, heros[:5], team_power(), flag_id)])
        session.send(OP_EQUIP_FLAG, body)
        push_flag_bonus_change()
        session.log("EquipFlag(旗帐%s -> 编队%s/%s playMode=%s) 成功" % (flag_id, category, index, cpm))
    elif opcode == OP_UNEQUIP_FLAG:
        # 军旗下阵（5354）：UnequipFlagFromBattleGameplayIndexRequest -> UpdateBattleGameplaySquadData
        if tmsg is not None:
            category, index, flag_id = (int(tmsg.category), int(tmsg.index or 0),
                                        int(tmsg.id or 0))
        else:
            category = int(_read_varint_field(pb, 1) or 0)
            index = int(_read_varint_field(pb, 2) or 0)
            flag_id = int(_read_varint_field(pb, 3) or 0)
        ensign.set_squad_flag(category, index, 0)
        save_player_data(PLAYER_DATA)
        heros = [(h["id"], h["position"]) for h in PLAYER_DATA.get("heroes", {}).values()
                 if h.get("position", -1) > 0]
        heros.sort(key=lambda x: x[1])
        session.send(OP_UNEQUIP_FLAG, build_update_squad_flag_data(
            category, 0, index, heros[:5], team_power(), 0))
        push_flag_bonus_change()
        session.log("UnequipFlag(编队%s/%s) 成功" % (category, index))
    elif opcode == OP_UNLOCK_NEW_FLAG:
        # 解锁旗帐（5356）：无 body -> FlagData（扣 1000 钻）
        ok, reason, flag = ensign.unlock_flag()
        if not ok:
            session.log("UnlockNewFlag 拒绝：%s" % reason)
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        session.send(OP_UPDATE_CHARACTER, update_character_body())
        session.send(OP_UNLOCK_NEW_FLAG, build_flag_data(flag))
        session.log("UnlockNewFlag -> 旗帐%s(index=%s) 余钻%s" % (
            flag["id"], flag["index"], PLAYER_DATA["player"]["diamond"]))
    elif opcode == OP_CHOOSE_FLAG_DRAW_MAP:
        # 跑商选图（31204）：IDListMessage{id=[mapId]} -> IDListMessage（客户端当 warMapNewTips）
        map_ids = req_id_list(tmsg, pb, _read_varint_fields)
        map_id = int(map_ids[0]) if map_ids else 0
        ok, reason = ensign.warbook_choose_map(map_id)
        if not ok:
            session.log("ChooseFlagDrawMap(%s) 拒绝：%s" % (map_id, reason))
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        session.send(OP_CHOOSE_FLAG_DRAW_MAP, f_int(1, map_id))
        session.log("ChooseFlagDrawMap -> 地图%s" % map_id)
    elif opcode == OP_START_FLAG_DRAW:
        # 开跑（31207）：无 body -> WarBookRecordData（校验马车等级 + 耗 1 张门票）
        ok, reason = ensign.warbook_start()
        if not ok:
            session.log("StartFlagDraw 拒绝：%s" % reason)
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        session.send(OP_START_FLAG_DRAW, build_warbook_record_data())
        session.send(OP_UPDATE_CHARACTER, update_character_body())   # 门票/铜币变化
        session.log("StartFlagDraw -> 开跑（map=%s）" % ensign.warbook_store().get("map_id"))
    elif opcode == OP_DIVINATION:
        # 前进/遇商人（31203）：无 body -> DivinationData{event, baguaStatus}
        ok, reason, event, cost, gained = ensign.warbook_divination()
        if not ok:
            session.log("Divination 拒绝：%s" % reason)
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        if cost:
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        session.send(OP_DIVINATION, build_divination_data(
            event, ensign.warbook_snapshot()))
        session.log("Divination -> 商人事件 获得=%s（耗币%s）" % (
            [(g["type"], g["value"]) for g in gained], cost))
    elif opcode == OP_DIVINATION_EVENT:
        # 领取商人事件（31208）：无 body -> DivinationResultData{event, divinationCount}
        ok, reason = ensign.warbook_event_done()
        if not ok:
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        wb = ensign.warbook_store()
        session.send(OP_DIVINATION_EVENT, build_divination_result(0, wb.get("divination_count")))
        session.log("DivinationEvent -> 领取完成 count=%s" % wb.get("divination_count"))
    elif opcode == OP_GET_FLAG_DRAW_REWARD:
        # 跑商结算（31206）：无 body -> WarBookRecordData（货物入包 + 马车经验）
        ok, reason, granted, lv = ensign.warbook_settle()
        if not ok:
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        _push_granted_items(session, [dict(a, kind="object") for a in granted])
        # ★ 跑商奖励弹窗走 301 正路（AwardMessageManager.parseAwardStr 弹
        #   AwardShow；连续跑商由客户端 ensignBusinessSuppressAwardShow 抑制）
        if granted:
            session.send(301, build_award_result("FlagDraw",
                                                 [(a["type"], a["count"], "") for a in granted]))
        session.send(OP_GET_FLAG_DRAW_REWARD, build_warbook_record_data())
        session.log("GetFlagDrawMapReward -> 入包%s 马车lv=%s" % (
            [(a["type"], a["count"]) for a in granted], lv))
    elif opcode == OP_GIVEUP_FLAG_DRAW:
        # 放弃跑商（31205）：无 body -> WarBookRecordData（货物保留一半）
        ok, reason, kept = ensign.warbook_giveup()
        if not ok:
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        _push_granted_items(session, [dict(a, kind="object") for a in kept])
        if kept:
            session.send(301, build_award_result("FlagDraw",
                                                 [(a["type"], a["count"], "") for a in kept]))
        session.send(OP_GIVEUP_FLAG_DRAW, build_warbook_record_data())
        session.log("GiveUpFlagDraw -> 保留%s" % [(a["type"], a["count"]) for a in kept])
    elif opcode == OP_REMOVE_MAP_REDPOINT:
        # 移除地图红点（31209）：IDListMessage{id=[...]} -> IDListMessage（剩余 tips）
        remove_ids = req_id_list(tmsg, pb, _read_varint_fields)
        tips = ensign.warbook_remove_tips(remove_ids)
        save_player_data(PLAYER_DATA)
        body = b""
        for t in tips:
            body += f_int(1, t)
        session.send(OP_REMOVE_MAP_REDPOINT, body)
        session.log("RemoveFlagDrawMapRedPoint(%s) -> tips=%s" % (remove_ids, tips))
    elif opcode == OP_BLUEPRINT_SYNTH:
        # 图纸合成（5355）：IDCounteListByIDMessage{id=配置ID, idCount=[{id=图纸type, count}]}
        # -> AwardResult（客户端 EnsignBlueprintMakePanel 自解播动画，不走 301）
        if tmsg is not None:
            make_id = int(tmsg.id or 0)
            id_counts = [{"id": int(e.id or 0), "count": int(e.count or 0)}
                         for e in tmsg.idCount]
        else:
            make_id = int(_read_varint_field(pb, 1) or 0)
            id_counts = []
            try:
                raw = pb.IDCounteListByIDMessage()
                raw.ParseFromString(pb)
                id_counts = [{"id": int(e.id or 0), "count": int(e.count or 0)}
                             for e in raw.idCount]
            except Exception:
                pass
        ok, reason, granted, consumed = ensign.blueprint_synthetic(make_id, id_counts)
        if not ok:
            session.log("BlueprintSynth(%s) 拒绝：%s" % (make_id, reason))
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        # 先推消耗图纸扣减（451，扣到 0 客户端 purgeZero 移除）
        _push_item_deltas(session, {a["type"]: -a["count"] for a in consumed})
        # 产出：新 type 401 / 已有 451
        _push_granted_items(session, [dict(a, kind="object") for a in granted])
        # ack = AwardResult（面板 onUseFlagPartBlueprintHandler 解此类型播动画）
        specs = [(a["type"], a["count"], "") for a in granted]
        session.send(OP_BLUEPRINT_SYNTH, build_award_result("", specs))
        session.log("BlueprintSynth(%s) 消耗=%s 产出=%s" % (
            make_id, [(a["type"], a["count"]) for a in consumed],
            [(a["type"], a["count"]) for a in granted]))
    elif opcode == OP_EVOLVE_HERO:
        # 升星：PropActionMessage{id=武将实例, targetID=圣兽实例} -> HeroData
        hid, mat_id = req_prop_action(tmsg, pb, _read_varint_field)
        hero = _find_hero(hid)
        mat = _find_hero(mat_id)
        htype = hero.get("type") if hero else 0
        session.log("EvolveHero(id=%s mat=%s) %s(Lv%s %s阶) + %s -> ？" % (
            hid, mat_id,
            game_config.get_hero_name(htype) if htype else "?",
            hero.get("lv") if hero else "?",
            hero.get("grade") if hero else "?",
            game_config.get_hero_name(mat.get("type")) if mat else "?"))
        ok, why = do_star_up(hero, mat)
        if ok:
            session.log("  升星成功 -> type=%s %s星 金币余 %s" % (
                hero["type"], hero.get("star"), PLAYER_DATA["player"]["coin"]))
            try:
                mission_achieve.bump_counter("compose")
                mission_achieve.push_updates(session)
                mission_periodic.push_refresh(session)
            except Exception as _e:
                flog("[mission] 合成计数失败: %r" % _e)
            save_player_data(PLAYER_DATA)
            session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
            session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
            if mat and mat.get("id"):
                session.send(OP_DELETE_PROP, f_int(1, 1) + f_int(2, mat["id"]))
        else:
            session.log("  升星被拒：%s" % why)
        session.send(OP_EVOLVE_HERO, build_hero_data(
            hid, hero["type"] if hero else 0,
            hero.get("lv", 1) if hero else 1,
            hero.get("position", 0) if hero else 0))
    elif opcode == OP_AWAKEN_HERO:
        # 突破：PropActionMessage{id=武将实例, targetID=同名6星卡实例} -> HeroData
        hid, mat_id = req_prop_action(tmsg, pb, _read_varint_field)
        hero = _find_hero(hid)
        mat = _find_hero(mat_id)
        session.log("AwakenHero(id=%s mat=%s) %s Lv%s 突破#%s" % (
            hid, mat_id,
            game_config.get_hero_name(hero.get("type")) if hero else "?",
            hero.get("lv") if hero else "?",
            hero.get("awaken") if hero else "?"))
        ok, why = do_awaken(hero, mat)
        if ok:
            session.log("  突破成功 -> 突破次数=%s 上限=%s" % (
                hero["awaken"],
                growth.level_cap(hero.get("star", 6), hero["awaken"],
                                 hero.get("can_break"))))
            save_player_data(PLAYER_DATA)
            session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
            session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
            if mat and mat.get("id"):
                session.send(OP_DELETE_PROP, f_int(1, 1) + f_int(2, mat["id"]))
        else:
            session.log("  突破被拒：%s" % why)
        session.send(OP_AWAKEN_HERO, build_hero_data(
            hid, hero["type"] if hero else 0,
            hero.get("lv", 1) if hero else 1,
            hero.get("position", 0) if hero else 0))
    elif opcode == OP_GRADE_HERO_LIST:
        # 升阶：IDListMessage{id=[武将, 材料1, 材料2...]} -> HeroData
        ids = req_id_list(tmsg, pb, _read_varint_fields)
        hid = ids[0] if ids else 0
        mat_ids = ids[1:]
        hero = _find_hero(hid)
        mats = [_find_hero(i) for i in mat_ids if _find_hero(i)]
        session.log("GradeHero(id=%s 材料=%s) %s %s阶%s点" % (
            hid, mat_ids,
            game_config.get_hero_name(hero.get("type")) if hero else "?",
            hero.get("grade") if hero else "?",
            hero.get("grade_exp") if hero else "?"))
        ok, why = do_grade_up(hero, mats)
        if not ok:
            # ★ 被拒必须告知玩家（与 5008 同一模式）：否则界面无任何反馈，
            #   且客户端本地材料移除会造成"材料凭空消失"的错觉（2026-10-21）
            # ★ 被拒不发 HeroData ack：客户端 ackShengjie 无条件触发面板"升阶成功"
            #   动画回调，失败时发 ack 会假装成功。不发 ack = 界面原样保留选材。
            session.log("  升阶被拒：%s" % why)
            session.send(5002, f_int(1, 0) + f_str(2, "升阶失败：%s" % why))
        if ok:
            session.log("  升阶成功 %s" % why)
            try:
                mission_achieve.bump_counter("grade")
                mission_achieve.push_updates(session)
                mission_periodic.push_refresh(session)
            except Exception as _e:
                flog("[mission] 升阶计数失败: %r" % _e)
            save_player_data(PLAYER_DATA)
            session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
            session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
            for _m in (mats or []):
                if _m and _m.get("id"):
                    session.send(OP_DELETE_PROP, f_int(1, 1) + f_int(2, _m["id"]))
            # ★ 响应挂 5021（GradeHeroRt），不是 5028 —— 见 OP_GRADE_HERO_RT 注释
            session.send(OP_GRADE_HERO_RT, build_hero_data(
                hid, hero["type"] if hero else 0,
                hero.get("lv", 1) if hero else 1,
                hero.get("position", 0) if hero else 0))
    elif opcode == OP_ADVANCE_EXP:
        # 一键提炼：IDMessage{id=武将实例} -> HeroData
        hid = req_id(tmsg, pb, _read_varint_field)
        hero = _find_hero(hid)
        session.log("AdvanceExp(id=%s) %s Lv%s exp%s" % (
            hid,
            game_config.get_hero_name(hero.get("type")) if hero else "?",
            hero.get("lv") if hero else "?",
            hero.get("exp") if hero else "?"))
        ok, why = do_advance_exp(hero)
        if ok:
            session.log("  升级成功 %s" % why)
            save_player_data(PLAYER_DATA)
            session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
            session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
        else:
            session.log("  升级被拒：%s" % why)
        session.send(OP_ADVANCE_EXP, build_hero_data(
            hid, hero["type"] if hero else 0,
            hero.get("lv", 1) if hero else 1,
            hero.get("position", 0) if hero else 0))
    elif opcode == OP_USE_FUNCTION:
        # 点具体某个符：FunctionData{id=物品ID, name=功能类名, count=数量, param=武将ID}
        # typed 路径：protobuf 库按 wire type 解，param（string 字段）直接
        # 拿到 "52" 再转数字；字节级回退只按 string 读（无 varint 回退，
        # 那个回退正是 2026-10-21「52 被读成 53」bug 的根）。等价性见
        # tools/golden_test.py 的 req_function 用例。
        item_id, fname, cnt, param = req_function(
            tmsg, pb, _read_varint_field, _read_string_field)
        # 卡实例 uid 必须在消耗前取出：do_use_* 内部会删除该实例
        inst_type = card_type_of(item_id)
        hero = _find_hero(param)
        session.log("UseFunction(item=%s name=%s count=%s hero=%s) %s Lv%s exp%s [pb=%s]" % (
            item_id, fname, cnt, param,
            game_config.get_hero_name(hero.get("type")) if hero else "?",
            hero.get("lv") if hero else "?",
            hero.get("exp") if hero else "?", pb.hex()))
        if fname == "AddProperty":
            ok, why = do_use_exp_item(hero, item_id, cnt)
        elif fname == "Synthetic":
            # ★ 碎片合成：客户端点碎片页签发的是 UseFunction(5008) name="Synthetic"，
            #   不是 5020。一键把该碎片能合的全部合掉。
            ok, why, new_ids = do_synthesize(item_id)
            if ok:
                save_player_data(PLAYER_DATA)
                # 同步碎片剩余数量
                left = int((PLAYER_DATA.get("items") or {}).get(str(item_id), 0))
                session.send(451, build_item_data(item_id, left))      # UpdateItem
                for nid in new_ids:
                    session.send(403, build_equip_data(find_equip(nid)))  # NewEquip
                # ★ 推 AwardResult(301) 触发「获得装备」弹窗
                _eq_types = []
                for nid in new_ids:
                    t = (find_equip(nid) or {}).get("type")
                    if t:
                        _eq_types.append((t, 1))
                if _eq_types:
                    session.send(301, build_award_result("Synthetic", _eq_types))
                session.send(OP_UPDATE_CHARACTER, update_character_body())
        elif fname == "SendAward":
            ok, why = do_use_gift(session, item_id, cnt)
        elif fname == "SelectAnyItem":
            ok, why = do_use_select_item(session, item_id, cnt, param)
        elif fname == "SelectHero":
            ok, why = do_use_select_hero(session, item_id, cnt, param)
        else:
            ok, why = False, "未实现的功能类型：%s" % fname
        if ok:
            session.log("  功能执行成功 %s" % why)
            save_player_data(PLAYER_DATA)
            # ★ 所有「消耗道具」的功能都要推 UpdateItem(451)，**含数量归零**：
            #   客户端 BagManager 收到 451 后 purgeZero 会把 count=0 的条目从列表移除。
            #   旧实现只在 AddProperty（经验符）分支推 → 随机材料箱/自选箱/选将卡用光后
            #   数量停在旧值、物品一直挂在军旗仓库里（2026-10-26 用户报障）。
            # ★ 卡实例（item_id 是 9xxxxxx uid）：各 do_use_* 内部已推 499 删格，
            #   这里不能再按 uid 当 type 推 451/499。
            if inst_type is None:
                push_item_count(session, item_id)
            # ★ 仅"用符升级武将"需要下面这一整段（推 453/战力）——
            #   开箱/自选箱/选将卡没有 param 武将，走这里会 hero=None 崩
            #   （2026-10-23 实测：开卡卡住报 TypeError）。
            if fname == "AddProperty":
                session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
                session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
                # 先推武将最新数据，回调里 self._data:lv() 才读得到新等级
                _hb = build_hero_data(
                    param, hero["type"], hero.get("lv", 1), hero.get("position", 0))
                _row = growth.find_hero_row(hero["type"]) or {}
                _cap = growth.level_cap(
                    int(growth._num(_row.get("Quality"))),
                    hero.get("awaken", 0), hero.get("can_break"))
                session.log("  推453 UpdateHero: id=%s lv=%s maxLv=%s exp=%s size=%d" % (
                    param, hero.get("lv"), _cap, hero.get("exp"), len(_hb)))
                session.send(OP_UPDATE_HERO, _hb)
        else:
            session.log("  功能执行被拒：%s" % why)
            # ★ 2026-10-21 经验符假「使用成功」修复：
            #   FunctionData 协议没有成功/失败字段，客户端 ackUseFunction 的回调
            #   （WarriorTrainPanel.usePotion）无条件弹「使用成功」。被拒时若照常回
            #   5008 ack，就会出现「显示使用成功但等级不变」。因此被拒时：
            #   ① 推 ShowMessage(5002) 把真实原因弹给客户端（MyApp.onShowMessageBox）
            #   ② 推 453 把服务端当前 lv/exp/maxLv 同步给客户端（纠正本地过期状态）
            #   ③ 不回 5008 ack —— 假「使用成功」不会再出现
            session.send(5002, f_int(1, 0) + f_str(2, why))
            if hero:
                _hb = build_hero_data(
                    param, hero["type"], hero.get("lv", 1), hero.get("position", 0))
                session.send(OP_UPDATE_HERO, _hb)
            return
        resp = b""
        resp += f_int(1, item_id)
        resp += f_str(2, fname)
        resp += f_int(3, cnt)
        resp += f_str(4, str(param))
        session.send(OP_USE_FUNCTION, resp)
    elif opcode == OP_WEAR_EQUIP:
        # 穿戴装备：PropActionMessage{id=装备实例, targetID=武将实例} -> EquipData
        eq_id, hero_id = req_prop_action(tmsg, pb, _read_varint_field)
        hero = _find_hero(hero_id)
        ok, why = do_wear_equip(eq_id, hero_id)
        session.log("WearEquip(equip=%s -> hero=%s) %s | %s" % (
            eq_id, hero_id, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
        # ★ 回包必须是 PropActionMessage{id=装备, targetID=武将}（客户端 ackEquip
        #   按此解码并拿 targetID 查武将）；回 EquipData 会把 type 当武将 id，
        #   showFatesTip 里 ipairs(nil) 崩。等价性/类型见 tools/equip_ack_test.py
        from . import builders_pb
        session.send(OP_WEAR_EQUIP, builders_pb.build_prop_action(eq_id, hero_id))
        if hero:
            # ★ 2026-10-28：改为无条件推（含 200）——只提供增伤/减伤这类
            #   不进战力权重的装备穿上后 power 不变，旧门槛会导致面板不更新
            session.send(OP_UPDATE_CHARACTER, update_character_body())
            session.send(OP_SQUAD_POWER, build_squad_power_change())
            session.send(OP_UPDATE_HERO, build_hero_data(
                hero_id, hero["type"], hero.get("lv", 1), hero.get("position", 0)))
    elif opcode == OP_DOWN_EQUIP:
        # 卸下装备：IDMessage{id=装备实例} -> EquipData
        eq_id = req_id(tmsg, pb, _read_varint_field)
        eq = find_equip(eq_id)
        host_id = int((eq or {}).get("host") or 0)   # 先记下原穿戴者，卸下后会被清 0
        ok, why = do_down_equip(eq_id)
        session.log("DownEquip(equip=%s) %s | %s" % (eq_id, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
            # ★ 回包是 IDMessage{id=装备}（客户端 ackUnEquip 按此解码）
            from . import builders_pb
            session.send(OP_DOWN_EQUIP, builders_pb.build_id_message(eq_id))
            host = _find_hero(host_id)
            if host:
                session.send(OP_UPDATE_CHARACTER, update_character_body())
                session.send(OP_SQUAD_POWER, build_squad_power_change())
                session.send(OP_UPDATE_HERO, build_hero_data(
                    host["id"], host["type"], host.get("lv", 1), host.get("position", 0)))
        else:
            session.send(OP_DOWN_EQUIP, b"")
    elif opcode in (OP_STRENGTHEN, OP_STRENGTHEN_AUTO):
        # 强化：5014 = IDMessage{id}（一次）；5016 = StrengthenEquip{typeID, times}（批量）
        if opcode == OP_STRENGTHEN:
            eq_id = req_id(tmsg, pb, _read_varint_field)
            times = 1
        else:
            eq_id, times = req_id_value(tmsg, pb, _read_varint_field)
            times = max(1, int(times or 1))
        eq = find_equip(eq_id)
        host = _find_hero((eq or {}).get("host") or 0)
        old_power = 0
        if host:
            st = hero_attrs(host.get("type"), host.get("lv", 1), host.get("grade", 0))
            apply_fate_to_stats(st, host)
            old_power = st.get("power", 0)
        ok, why, eq = do_strengthen(eq_id, times)
        session.log("Strengthen(equip=%s times=%d) %s | %s" % (eq_id, times, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
        # ★ 批量强化必须回 5015（OneKeyStrengthenRt -> ackStrengthEquipAuto），
        #   不能回 5016（那一格被 ackUnlockEquip「解锁副属性」占用，会解码错乱→崩）
        reply_op = OP_STRENGTHEN if opcode == OP_STRENGTHEN else OP_STRENGTHEN_AUTO_RT
        # ★ 200 必须排在 ack **之前**（同 6021 升阶）：客户端 EquipStrengthPanel
        #   成功回调 self:refresh({}) → updateLabel() → _costLabel:updateData(
        #   me:coin(), cost) 在 ack 当时重读本地币值；ack 先到就画旧币，
        #   200 后到没人再刷面板（2026-10-28 用户报障的同一根因）。
        #   只要扣了铜币就必须推 200（界面铜币=updateMyData 读 userData.coin），
        #   不能挂在「战力变化」上：强化不动面板上的未穿戴装备时战力不变，
        #   旧门槛下钱扣了但 200 不发 → 界面铜币不刷新（2026-10-22 用户报障）
        if ok:
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        session.send(reply_op, build_equip_data(eq))
        if ok:
            push_equip_host(session, eq)   # ★ 无条件推 453：属性面板必须跟着变
    elif opcode == OP_FASTEN_FATE:
        # 强化缘分：IDListMessage{id={缘分拥有者实例, 材料}} -> FateFastenInfo
        ids = req_id_list(tmsg, pb, _read_varint_fields)
        owner_id = ids[0] if ids else 0
        mat_id = ids[1] if len(ids) > 1 else 0
        # 必须在消耗前解析（卡实例被删后 card_type_of 就查不到了）
        _mat_type = resolve_item_type(mat_id)
        _mat_uid = int(mat_id) if card_type_of(mat_id) else None
        hero = _find_hero(owner_id)
        old_power = 0
        if hero:
            st = hero_attrs(hero.get("type"), hero.get("lv", 1), hero.get("grade", 0))
            apply_fate_to_stats(st, hero)
            old_power = st.get("power", 0)
        session.log("FastenFate(owner=%s mat=%s) %s" % (
            owner_id, mat_id,
            game_config.get_hero_name(hero.get("type")) if hero else "?"))
        ok, why, touched, consumed_hero_id = do_fasten_fate(hero, mat_id)
        if ok:
            session.log("  缘分强化成功 %s" % why)
            save_player_data(PLAYER_DATA)
            session.send(OP_UPDATE_CHARACTER, update_character_body())  # 同步玩家战力/货币
            session.send(OP_SQUAD_POWER, build_squad_power_change())  # 主界面/布阵战力刷新
        else:
            session.log("  缘分强化被拒：%s" % why)
            # 被拒必须给玩家可见提示（铜币不足/材料不匹配等），否则只看到
            # "点了没反应、卡还在"（与 5008/5028/6021 同模式）
            session.send(5002, f_int(1, 0) + f_str(2, why))

        new_power = old_power
        if hero:
            st2 = hero_attrs(hero.get("type"), hero.get("lv", 1), hero.get("grade", 0))
            apply_fate_to_stats(st2, hero)
            new_power = st2.get("power", 0)

        # ★ 推送顺序很关键：先把「武将最新数据」和「删卡」推下去，
        #   最后再回 20300 —— 客户端 ackFastenFate 的回调里会立刻 refresh 面板，
        #   如果 453/499 在它后面到，回调拿到的还是旧数据。
        # 材料是缘分卡（替身卡，道具）时同样要同步背包：FatePanel 的材料列表来自
        # bagManager:getRDCardList()，不推 451/499 就会一直显示已消耗的卡
        # （2026-10-26 用户报障：武将材料会及时消失，缘分卡不会）。
        if ok and growth.fate_item_info(_mat_type):
            if _mat_uid:
                # 卡实例整张消耗 → 499 删掉该卡格（每张卡一个唯一 id）
                session.send(OP_DELETE_PROP, f_int(1, 2) + f_int(2, _mat_uid))
            else:
                _inv = PLAYER_DATA.get("items") or {}
                _left = int(_inv.get(str(int(mat_id)), 0))
                if _left > 0:
                    session.send(451, build_item_data(int(mat_id), _left))
                else:
                    session.send(OP_DELETE_PROP, f_int(1, 2) + f_int(2, int(mat_id)))
        if consumed_hero_id:
            session.send(OP_DELETE_PROP, f_int(1, 1) + f_int(2, consumed_hero_id))
        if hero and (ok or new_power != old_power):
            # ★ 2026-10-28 修：缘分永久激活按「同名 Group」对**全部实例**生效，
            #   但这里原来只重推 owner 一个实例的 453 —— 其它同名实例的面板
            #   停留在激活前的旧数值（用户报障：无双吕布激活无双血脉后，
            #   另一个同名实例属性表没有缘分加成，重登才对）。
            #   同名全部实例统一重推 453（数据本来就算对了，缺的是推送）。
            _g = growth.hero_group(hero["type"])
            for _hid, _h in (PLAYER_DATA.get("heroes") or {}).items():
                if growth.hero_group(_h.get("type")) == _g:
                    session.send(OP_UPDATE_HERO, build_hero_data(
                        int(_hid), _h["type"], _h.get("lv", 1),
                        _h.get("position", 0)))

        body = b""
        # 注意：build_fate_info() 内部已经逐条写了 `f_msg(1, ...)`（repeated fateInfo），
        # 这里**不能再套一层 f_msg(1, ...)**，否则客户端解出来的 FateRecordData 是坏数据，
        # 表现就是"强化成功但进度还是 0"。
        body += build_fate_info()               # fateInfo（字段1，repeated）
        body += f_int(3, owner_id)              # targetHeroID
        body += f_int(4, old_power)             # targetOldPower
        body += f_int(5, new_power)             # targetNewPower
        if touched:
            body += f_int(6, touched[0])        # targetFateType
        session.send(OP_FASTEN_FATE, body)
    elif opcode == OP_GRADE_UP_EQUIP:
        # 装备升阶：IDListMessage{id=[装备, 材料1, 材料2, ...]}
        ids = req_id_list(tmsg, pb, _read_varint_fields)
        eq_id = ids[0] if ids else 0
        mat_ids = ids[1:]
        eq = find_equip(eq_id)
        host = _find_hero((eq or {}).get("host") or 0)
        old_power = 0
        if host:
            st = hero_attrs(host.get("type"), host.get("lv", 1), host.get("grade", 0))
            apply_fate_to_stats(st, host)
            old_power = st.get("power", 0)
        ok, why, eq = do_grade_up_equip(eq_id, mat_ids)
        session.log("GradeEquip(equip=%s mats=%s) %s | %s" % (eq_id, mat_ids, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
            for mid in mat_ids:
                session.send(OP_DELETE_PROP, f_int(1, 3) + f_int(2, int(mid)))  # typeID=3 装备（客户端 BagManager.onDelProp: 2=道具 3=装备）
            # ★ 200 必须排在 6021 ack **之前**：客户端 EquipShengJiePanel 成功
            #   回调 self:refresh({clearPick=true}) → _costLabel:updateData(
            #   me:coin(), cost) 会重读本地币值渲染「当前/花费」；ack 先到就把
            #   进门时的旧币值画上去，200 后到也没人再刷这个面板 → 按钮上的
            #   铜钱不刷新（2026-10-28 用户报障：原版点完就刷新）。
            #   洗练 6025「451→200→ack」同此帧序。
            session.send(OP_UPDATE_CHARACTER, update_character_body())
            # 成功才发 ack（6021 EquipData）
            session.send(OP_GRADE_UP_EQUIP, build_equip_data(eq))
        else:
            # ★ 被拒不发 ack（与 5008/5028 同模式）：客户端面板回调不清会挂着，
            #   但 5002 必须告知原因；ack 若带空 EquipData 曾炸 dispatch
            session.send(5002, f_int(1, 0) + f_str(2, "装备升阶失败：%s" % why))
        if ok:
            push_equip_host(session, eq)   # ★ 无条件推 453：属性面板必须跟着变
    elif opcode == OP_STAR_UP_EQUIP:
        # 装备升星：IDListMessage{id=[装备, 材料1, 材料2]}
        ids = req_id_list(tmsg, pb, _read_varint_fields)
        eq_id = ids[0] if ids else 0
        mat_ids = ids[1:]
        eq = find_equip(eq_id)
        host = _find_hero((eq or {}).get("host") or 0)
        old_power = 0
        if host:
            st = hero_attrs(host.get("type"), host.get("lv", 1), host.get("grade", 0))
            apply_fate_to_stats(st, host)
            old_power = st.get("power", 0)
        ok, why, eq = do_star_up_equip(eq_id, mat_ids)
        session.log("StarUpEquip(equip=%s mats=%s) %s | %s" % (eq_id, mat_ids, ok, why))
        if not ok:
            # ★ 拒绝必须给玩家反馈（同 5008/5347 口径）：以前只 log，客户端
            #   静默播完升星动画 → 玩家以为成功了（2026-10-28 报障次因）。
            session.send(5002, f_int(1, 0) + f_str(2, why))
        if ok:
            save_player_data(PLAYER_DATA)
            for mid in mat_ids:
                session.send(OP_DELETE_PROP, f_int(1, 3) + f_int(2, int(mid)))  # typeID=3 装备（客户端 BagManager.onDelProp: 2=道具 3=装备）
            # ★ 2026-10-28 用户报障「升星后客户端还显示 4 星」：
            #   升星换的是**装备 type**（4星形态→5星形态），但客户端所有更新入口
            #   （6023 ack / 452 UpdateEquip / 467·472 批量）都只走
            #   Equip:updateWithSvrData —— 只更新 host/强化/属性/阶，**从不更新
            #   type/_star**（星级、品质、部位都来自 EquipConfig，创建时定死）。
            #   唯一可靠路径：先删同 id 旧实例（499 DeleteProp typeID=3），再按新
            #   type 推 403 NewEquip —— 客户端 addSvrEquip 里 getEquip(id) 为空才会
            #   走 Equip:type(新 type) 重建对象，星级/品质/部位一并刷新。
            #   顺序必须是 499 → 403 → 6023 ack（ack 里 getEquip 拿到的才是新对象，
            #   playAnimation 用的 star 才是 5）。
            session.send(OP_DELETE_PROP, f_int(1, 3) + f_int(2, int(eq_id)))
            session.send(403, build_equip_data(eq))   # NewEquip（客户端重建实例）
        session.send(OP_STAR_UP_EQUIP, build_equip_data(eq))
        # ★ 扣铜币成功即推 200（不能挂战力变化门槛，同强化）
        if ok:
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        if ok:
            push_equip_host(session, eq)   # ★ 无条件推 453：属性面板必须跟着变
    elif opcode in (OP_REFORM, OP_SAVE_REFORM, OP_GIVEUP_REFORM):
        # 改造三步：6025 洗练 / 6026 保存 / 6027 放弃
        # 请求可能是 IDListMessage{id=[装备, 槽位]} 或 IDMessage{id=装备}
        ids = req_id_list(tmsg, pb, _read_varint_fields)
        eq_id = ids[0] if ids else 0
        slot = ids[1] if len(ids) > 1 else 1
        if opcode == OP_REFORM:
            ok, why, eq = do_reform_equip(eq_id, slot, int(PLAYER_DATA.get("player", {}).get("lv") or 60))
        elif opcode == OP_SAVE_REFORM:
            ok, why, eq = do_save_reform(eq_id, slot)
        else:
            ok, why, eq = do_giveup_reform(eq_id, slot)
        session.log("EquipReform(op=%d equip=%s slot=%s) %s | %s" % (opcode, eq_id, slot, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
            # 洗练消耗铜币+洗练石 → 200 刷货币、451 刷石头
            if opcode == OP_REFORM:
                _inv = PLAYER_DATA.get("items") or {}
                session.send(451, build_item_data("20002", int(_inv.get("20002", 0))))
                session.send(OP_UPDATE_CHARACTER, update_character_body())
                # ★ 客户端【没有注册 6025 的 handler】（只注册了 6026/6027）——
                #   6025 的 ack 会被静默丢弃 → 面板不刷新 → “点了没反应”、
                #   待保存数值/颜色分档/保存门禁全部不更新，而服务端每次都真实扣费。
                #   原版做法应是借 6026 帧驱动刷新：onEquipSaveReformHandler 会
                #   onUpdateEquip（数据更新）+ panel:refresh（面板重绘，读到
                #   lastUnlockAssistant 的待保存状态 → 颜色/门禁生效）。
                session.send(OP_SAVE_REFORM, build_equip_data(eq))
            session.send(opcode, build_equip_data(eq))
            # ★ 6026 保存后副属性真正生效 → 必须推 453（6025 只是 pending，不推）
            if opcode == OP_SAVE_REFORM and eq:
                push_equip_host(session, eq)
        else:
            # 被拒（铜币/洗练石不足、同属性数值更低不许保存…）→ 5002 告知原因，
            # 不发 ack；状态未变无需面板刷新（与 5008/5028/6021 同模式）
            session.send(5002, f_int(1, 0) + f_str(2, "洗练操作失败：%s" % why))
    elif opcode == 5210:
        # BuyProp（IDCountMessage{id, count}）——商城购买/充值
        # 客户端 ackBuyProp 解 AwardResult 弹窗（parseAwardStr）
        _id, _cnt = req_id_count(tmsg, pb, _read_varint_field)
        ok, why, awards, extra = do_buy_prop(_id, _cnt or 1)
        session.log("BuyProp(id=%s x%s) %s | %s" % (_id, _cnt, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
            flush_now()      # 充值/购买：立刻落库
            from . import builders_pb
            session.send(5210, builders_pb.build_award_result("BuyProp", _awards_to_specs(awards)))
            # 月卡/周卡：590 新建记录（首次）/ 591 更新剩余天数（续费）
            _bp_rec = extra.get("buy_prop_record")
            if _bp_rec:
                session.send(
                    OP_NEW_BUY_PROP_RECORD if extra.get("buy_prop_new")
                    else OP_UPDATE_BUY_PROP_RECORD,
                    builders_pb.buy_prop_record_msg(_bp_rec).SerializeToString())
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == OP_SIGN:
        # 签到（10003，空 body）→ 发奖 + 5002 说明 + 451 道具 + 200 签到字段同步
        ok, why, awards = do_sign()
        session.log("Sign() %s | %s" % (ok, why))
        if ok:
            names = _grant_sign_awards(awards)
            save_player_data(PLAYER_DATA)
            _inv = PLAYER_DATA.get("items") or {}
            for a in awards:
                if a.get("kind") == "object":
                    session.send(451, build_item_data(str(int(a["type"])),
                                                      int(_inv.get(str(int(a["type"])), 0))))
            # ★ 奖励弹窗走 8000+AwardResult（客户端 getExchangeAwardHandler →
            #   parseAwardStr → AwardShow 弹框，并自带 SignInPanel 刷新）——
            #   不发 5002 文字（避免双弹窗）
            from . import builders_pb
            session.send(301, builders_pb.build_award_result("Sign", _awards_to_specs(awards)))
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == OP_BUY_SIGN:
        # 补签（10004，空 body）：VIP5 + 漏签 + 元宝；领下一天奖励
        ok, why, awards = do_buy_sign()
        session.log("BuySign() %s | %s" % (ok, why))
        if ok:
            names = _grant_sign_awards(awards)
            save_player_data(PLAYER_DATA)
            _inv = PLAYER_DATA.get("items") or {}
            for a in awards:
                if a.get("kind") == "object":
                    session.send(451, build_item_data(str(int(a["type"])),
                                                      int(_inv.get(str(int(a["type"])), 0))))
            from . import builders_pb
            session.send(301, builders_pb.build_award_result("BuySign", _awards_to_specs(awards)))
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == 31100:
        # DoActivityInteraction（ActivityInteractionData{ActivityID, Action, TargetID}）
        # 幸运转盘：Action=1 抽奖 → 推 31200；Action=0 买钥匙(TargetID=数量) → 推 31201
        # ★ 31100 本身不回 body（客户端无 handler）
        from .activities import turntable
        # ActivityInteractionData{ActivityID=1, Action=2, TargetCategory=3, TargetID=4, Param=5}
        # tmsg 是 parse_typed 的对象，可能为 None；统一从原始字节 pb 解析。
        # ★ Action=0（买钥匙）是 proto 默认值，pbc 序列化会**省略该字段**——
        #   读不到 Action 时用 TargetID 判别：带 TargetID=buycount → 买钥匙，否则抽奖。
        _aid = _read_varint_field(pb, 1) or 0
        _act = _read_varint_field(pb, 2)
        _tid = _read_varint_field(pb, 4) or 0
        if _act is None:
            _action = 0 if _tid > 0 else 1
        else:
            _action = _act
        session.log("DoActivityInteraction(activity=%s action=%s target=%s)" % (_aid, _action, _tid))
        if _action == 1:
            ok, why, idx0, award, keys = turntable.draw()
            session.log("  转盘抽奖 %s | %s idx=%s keys=%s" % (ok, why, idx0, keys))
            if ok:
                save_player_data(PLAYER_DATA)
                session.send(31200, turntable.build_result(int(_aid or 701), idx0, keys, award))
            else:
                session.send(5002, f_int(1, 0) + f_str(2, why))
        else:
            ok, why, keys = turntable.buy_keys(_tid or 1)
            session.log("  买钥匙 %s | %s keys=%s" % (ok, why, keys))
            if ok:
                save_player_data(PLAYER_DATA)
                session.send(31201, f_int(1, int(keys)))     # IDMessage{id=keyCount}
                push_total_consume(session)
                session.send(OP_UPDATE_CHARACTER, update_character_body())
            else:
                session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == 5314:
        # BuySecretProp（IDMessage{id=商品index}）→ 5314 空回 + 5317 整包
        from .activities import secret_shop
        _pidx = req_id(tmsg, pb, _read_varint_field)
        ok, why, awards, prop = secret_shop.buy(_pidx)
        session.log("BuySecretProp(index=%s) %s | %s" % (_pidx, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
            session.send(5314, b"")       # 空 ack（客户端不解包，本地置已售）
            session.send(5317, secret_shop.snapshot().SerializeToString())
            _push_granted_items(session, awards)   # ★ 401/451：背包更新（缺失则道具不可见）
            push_total_consume(session)
            session.send(301, build_award_result_proxy("SecretShop", awards))
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == 5357:
        # BuyActivityShop（ActivityShopMessage{id, activityShopChoose[{type,idList}]}）
        # → 5357 回 GradeExchangeData 单档（Grade 必须回显）
        from .activities import activity_shop
        _grade = req_id(tmsg, pb, _read_varint_field)
        # choose 列表：ActivityShopMessage.activityShopChoose (field 2, repeated)
        _chooses = []
        for _ch in getattr(tmsg, "activityShopChoose", []) or []:
            _chooses.append({"type": int(_ch.type), "idList": [int(x) for x in _ch.idList]})
        ok, why, awards, body = activity_shop.buy(_grade, _chooses)
        session.log("BuyActivityShop(grade=%s choose=%s) %s | %s"
                    % (_grade, len(_chooses), ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
            session.send(5357, body)
            # ★ 301 物品获得弹窗：真服行为（用户实证 APK 弹过）——BestDeal 兑换后弹
            session.send(301, build_award_result_proxy("BestDeal", awards))
            _push_granted_items(session, awards)   # 401/451：背包更新
            push_total_consume(session)
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == 5312:
        # GetSinglePayAward（充值自选礼包 PayChose 领奖）——客户端 ackSinglePay
        #   解 **SinglePayActivityData**，与该活动在 5311 的 singlePay 字段配套。
        #   该活动已砍掉（5311 显式下发 singlePay.isStart=-1，服务端不产生
        #   SinglePayActivityData），不能借用单笔充值的 one_pay.claim 顶替：
        #   回包类型与客户端解码类型不一致（GradeTimesStatusData ≠
        #   SinglePayActivityData）→ 统一按"活动未开启"回 5002。
        session.log("GetSinglePayAward 被拒：充值自选礼包活动未开启")
        session.send(5002, f_int(1, 0) + f_str(2, "充值自选礼包活动未开启"))
    elif opcode == 5322:
        # GetOnePayAward（IDMessage{id=grade}）——客户端 SinglePayCell 的领取入口
        # 回 GradeTimesStatusData 单档（ackOnePay 就是按这个类型 decode，
        # 不是 OnePayActivityData 全量）
        from .activities import one_pay
        _gid = req_id(tmsg, pb, _read_varint_field)
        ok, why, awards = one_pay.claim(_gid)
        session.log("GetOnePayAward(grade=%s) %s | %s" % (_gid, ok, why))
        if ok:
            _grant_sign_awards(awards)
            save_player_data(PLAYER_DATA)
            session.send(5322, one_pay.build_claim_ack(_gid))
            session.send(301, build_award_result_proxy("OnePay", awards))
            # ★ 普通道具（装备卡/材料）必须推 401/451，否则背包看不到、要重登才显示
            #   （2026-10-27 用户报障：单笔充值领的装备卡背包里没有）
            _push_granted_items(session, awards)
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == 5323:
        # GetTotalPayAward（IDMessage{id=Grade}）→ GradStatusData（该档新状态）
        from .activities import grade_pay
        _gid = req_id(tmsg, pb, _read_varint_field)
        ok, why, awards = grade_pay.claim("total_pay", "dollar", _gid)
        session.log("GetTotalPayAward(grade=%s) %s | %s" % (_gid, ok, why))
        if ok:
            _grant_sign_awards(awards)
            save_player_data(PLAYER_DATA)
            session.send(301, build_award_result_proxy("TotalPay", awards))
            _push_granted_items(session, awards)   # 401/451：同 5322，普通道具必须推
            session.send(5323, _grade_status_body("total_pay", "dollar", int(_gid)))
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == 5313:
        # GetTotalConsumeAward（IDMessage{id=Grade}）→ GradStatusData
        from .activities import grade_pay
        _gid = req_id(tmsg, pb, _read_varint_field)
        ok, why, awards = grade_pay.claim("total_consume", "consume", _gid)
        session.log("GetTotalConsumeAward(grade=%s) %s | %s" % (_gid, ok, why))
        if ok:
            _grant_sign_awards(awards)
            save_player_data(PLAYER_DATA)
            session.send(301, build_award_result_proxy("TotalConsume", awards))
            _push_granted_items(session, awards)   # 401/451
            session.send(5313, _grade_status_body("total_consume", "consume", int(_gid)))
            push_total_consume(session)
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == 10005:
        # GetOnlineAward（空 body）→ OnlineAwardRecordData（新档；M4）
        from .activities import online_award
        ok, why, awards = online_award.claim()
        session.log("GetOnlineAward() %s | %s" % (ok, why))
        if ok:
            _grant_sign_awards(awards)
            save_player_data(PLAYER_DATA)
            session.send(10005, online_award.snapshot().SerializeToString())
            session.send(301, build_award_result_proxy("OnlineAward", awards))
            _push_granted_items(session, awards)   # 401/451：同 5322
            # ★ 2026-10-28：属性奖励（铜钱/元宝）必须推 200，否则主界面货币不刷新
            #   （用户报障："领取铜钱主界面显示铜钱的地方没刷新"）
            session.send(OP_UPDATE_CHARACTER, update_character_body())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == 5324:
        # GetLoginDaysAward（IDMessage{id=天数}）→ LoginDaysActivityData
        from .activities import login_days
        _gid = req_id(tmsg, pb, _read_varint_field)
        ok, why, awards = login_days.claim(_gid)
        session.log("GetLoginDaysAward(day=%s) %s | %s" % (_gid, ok, why))
        if ok:
            _grant_sign_awards(awards)
            save_player_data(PLAYER_DATA)
            session.send(301, build_award_result_proxy("LoginDays", awards))
            _push_granted_items(session, awards)
            session.send(OP_UPDATE_CHARACTER, update_character_body())  # 属性奖励刷货币
            ld_snap = login_days.snapshot()
            if ld_snap is not None:
                session.send(5324, ld_snap.SerializeToString())
        else:
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == 5311:
        # GetActivityGroup（活动系统主拉取，空 body）→ ActivityGroupData
        from . import activities
        _body = activities.build_activity_group()
        try:
            from ..proto import ps_protobuf_pb2 as _pb
            _m = _pb.ActivityGroupData(); _m.ParseFromString(_body)
            _tt = _m.lotteryDrawActivity
            session.log("5311 转盘: ID=%s Cells=%d Currencies=%s Times=%s Indexes=%s keyCount=%s currency=%s price=%s"
                        % (_tt.ID, len(_tt.Cells),
                           [(c.id, c.count) for c in _tt.Currencies],
                           _tt.Times, list(_tt.Indexes), _tt.keyCount, _tt.currency, _tt.price))
        except Exception as _e:
            session.log("5311 dump 失败: %s" % _e)
        session.log("GetActivityGroup() -> ActivityGroupData")
        session.send(5311, _body)
    elif opcode == OP_DECOMPOSE:
        # 分解：IDListMessage{id=[装备...]}，只收未穿戴的
        ids = req_id_list(tmsg, pb, _read_varint_fields)
        ok, why, _ = do_decompose_equips(ids)
        session.log("DecomposeEquips(%s) %s | %s" % (ids, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
            for mid in ids:
                session.send(OP_DELETE_PROP, f_int(1, 3) + f_int(2, int(mid)))  # typeID=3 装备
            # 分解返还洗练石 → 推 UpdateItem(451)
            _inv = PLAYER_DATA.get("items") or {}
            session.send(451, build_item_data("20002", int(_inv.get("20002", 0))))
            session.send(OP_UPDATE_CHARACTER, update_character_body())
            session.send(OP_SQUAD_POWER, build_squad_power_change())
        session.send(OP_DECOMPOSE, b"")
    elif opcode == OP_SYNTHETIC:
        # 碎片合成：IDMessage{id=碎片道具ID}，一键合成能合的全部
        frag_id = req_id(tmsg, pb, _read_varint_field)
        ok, why, new_ids = do_synthesize(frag_id)
        session.log("Synthetic(frag=%s) %s | %s" % (frag_id, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
            session.send(OP_UPDATE_CHARACTER, update_character_body())
            for nid in new_ids:
                session.send(403, build_equip_data(find_equip(nid)))  # NewEquip 推送
        session.send(OP_SYNTHETIC, b"")
    elif opcode == OP_UNLOCK_ASSIST:
        # 解锁副属性：IDListMessage{id=[装备, 槽位]}（耗 300 元宝）
        ids = req_id_list(tmsg, pb, _read_varint_fields)
        eq_id = ids[0] if ids else 0
        slot = ids[1] if len(ids) > 1 else 1
        ok, why, eq = do_unlock_assistant(
            eq_id, slot, int(PLAYER_DATA.get("player", {}).get("lv") or 60))
        session.log("UnlockAssistant(equip=%s slot=%s) %s | %s" % (eq_id, slot, ok, why))
        if ok:
            save_player_data(PLAYER_DATA)
            session.send(OP_UPDATE_CHARACTER, update_character_body())
            push_equip_host(session, eq)   # ★ 新副属性生效 → 推 453/20416
        session.send(OP_UNLOCK_ASSIST, build_equip_data(eq))
    elif opcode == OP_LOTTERY_RECORDS:
        # 5041 GetLotteryRecords（空 body）→ LotteryRecordDatas
        session.log("GetLotteryRecords() -> %d 条" % len(lottery_records_snapshot()))
        session.send(OP_LOTTERY_RECORDS, builders_lottery_records())
    elif opcode == OP_LOTTERY:
        # 5040 UseLottery：招募（476 武将）/ 寻宝（5040 AwardResult）
        rid, use_item = req_use_lottery(tmsg, pb, _read_varint_field)
        ok, why, res = do_lottery(rid, use_item)
        _rec = _lottery_store().get(str(int(rid or 0))) or {}
        session.log("UseLottery(id=%s type=%s useItem=%s) %s | %s" % (
            rid, _rec.get("type"), use_item, ok, why))
        if not ok:
            # 客户端没有「失败回包」通道（会锁输入），所以补一个 5002 说明原因；
            # 客户端 override 的看门狗会在超时后自动解锁/关面板兜底。
            session.send(5002, f_int(1, 0) + f_str(2, why))
        else:
            from . import builders_pb as _b
            plan = res.get("plan") or {}
            # ★ 扣费同步必须排在抽卡结果前面：
            #   客户端 EmployeeManager 收到 476 后**同步**执行结果面板回调，
            #   EmployeeResultPanel:refreshSingleRecruitCosts -> showDollarEmployeePrice
            #   会用 bagManager 当前招募令数量渲染「再招一次/再招10次」角标。
            #   如果 451/删除道具排在 476 后面，角标拿到的是扣费前的数量
            #   （20 抽完显示 20，实际只剩 10），2026-10-26 用户报障。
            if plan.get("item"):
                tok = int(plan["item"])
                _inv = PLAYER_DATA.get("items") or {}
                _left = int(_inv.get(str(tok), 0))
                if _left > 0:
                    session.send(451, build_item_data(tok, _left))
                else:
                    # 令牌用完：删掉道具，否则客户端会显示 0/1 并盖住元宝价格
                    session.send(OP_DELETE_PROP, f_int(1, 2) + f_int(2, tok))
            if res.get("category") == "hero":
                items = [(404, build_hero_data(hid, htype, 1, 0))
                         for hid, htype in res.get("heroes", [])]
                session.send(476, _b.build_all_hero_message(items))
                names = ",".join("%s(%s)" % (htype, hid) for hid, htype in res.get("heroes", []))
                session.log("  招募产出: %s" % names)
                # ---- 成就刷新：获得武将（target=9 周期 + 终身链）----
                try:
                    mission_achieve.push_updates(session)
                    mission_periodic.push_refresh(session)
                except Exception as _e:
                    flog("[mission] 获得武将刷新失败: %r" % _e)
            else:
                awards = res.get("awards") or []
                # 装备先入库（403 NewEquip），再回 AwardResult 给结果面板
                for a in awards:
                    if a.get("kind") == "equip":
                        session.send(403, build_equip_data(find_equip(a["id"])))
                specs = [(a["type"], a.get("count", 1), a.get("name", "")) for a in awards]
                session.send(OP_LOTTERY, build_award_result("Explore", specs))
                _inv = PLAYER_DATA.get("items") or {}
                for t in sorted({a["type"] for a in awards if a.get("kind") == "item"}):
                    session.send(451, build_item_data(t, int(_inv.get(str(t), 0))))
                session.log("  寻宝产出: %s" % ",".join(
                    "%s×%s" % (a.get("name"), a.get("count")) for a in awards))
            if plan.get("free"):
                session.log("  本次免费，下次免费时间戳=%s" % plan.get("next_free"))
            else:
                session.log("  扣费: %s" % (plan,))
            session.send(OP_UPDATE_CHARACTER, update_character_body())
    elif opcode == OP_COLLECT_AWARD:
        # 10115 图鉴里程碑领取
        ctype, subtype, milestone = req_collection_award(tmsg, pb, _read_varint_field)
        ok, why, res = do_receive_collection_award(ctype, subtype, milestone)
        session.log("ReceiveCollectionAward(type=%s subType=%s milestone=%s) %s | %s" % (
            ctype, subtype, milestone, ok, why))
        from . import builders_pb as _b
        if ok:
            session.send(OP_COLLECT_AWARD, _b.build_collection_award_record(
                ctype, subtype, res.get("milestones")))
            # 奖励弹窗：301 AwardResult（客户端 AwardMessageManager 统一处理）
            session.send(301, build_award_result("Collection", _collection_award_specs(
                res.get("awards") or [])))
            _inv = PLAYER_DATA.get("items") or {}
            for a in (res.get("awards") or []):
                if a.get("kind") == "object" and str(int(a.get("type") or 0)) in _inv:
                    # ★ 2026-10-28 修：items 的键是字符串（"20010"），旧代码用
                    #   int 查 `in _inv` 恒为 False → 451 从不推送 → 弹窗显示
                    #   获得了物品但背包数量不刷新（用户报障「图鉴奖励未到账」，
                    #   实际已入库，重登才显示）。
                    session.send(451, build_item_data(
                        a["type"], int(_inv.get(str(a["type"]), 0))))
            session.send(OP_UPDATE_CHARACTER, update_character_body())
            session.log("  领取成功: %s" % "、".join(res.get("desc") or []))
        else:
            # ★ 失败也要回 10115：客户端 onReceiveCollectionAward 靠它清
            #   _requestingCollectionAward，否则同一里程碑会一直点不动。
            _c = _collect_store()
            _ms = (_c.get("awards") or {}).get("%d_%d" % (ctype, subtype), [])
            session.send(OP_COLLECT_AWARD, _b.build_collection_award_record(
                ctype, subtype, _ms))
            session.send(5002, f_int(1, 0) + f_str(2, why))
    elif opcode == OP_SUBMIT_MISSION:
        # 领奖（10100，IDMessage{id=任务实例id}）—— 客户端的领取按钮走这里，不是 504
        _mid = req_id(tmsg, pb, _read_varint_field)
        ok, reason, node, awards = mission.claim(_mid)
        if not ok:
            # 主线领不了 → 试「每日扫荡令牌」限时任务（2026-10-29）
            _tok_ok, _tok_reason, _tok_row, _tok_awards = daily_token.claim(_mid)
            if _tok_ok:
                _desc = _grant_sign_awards(_tok_awards)
                _push_granted_items(session, _tok_awards)
                save_player_data(PLAYER_DATA)
                session.send(OP_MISSION_SUCCESS, build_mission_success(
                    int(_tok_row["id"]), int(_tok_row["id"]),
                    build_award_result_proxy("Mission", _tok_awards)))
                session.send(OP_DELETE_MISSION, f_int(1, int(_tok_row["id"])))
                session.send(OP_UPDATE_CHARACTER, update_character_body())
                session.log("SubmitMission(%s) 每日令牌任务 → %s" % (_mid, "、".join(_desc)))
                return
            # 再试成就/收集类任务（星星收集狂/等级礼包/大师之路…，2026-10-29）
            _a_ok, _a_reason, _a_row, _a_awards = mission_achieve.claim(_mid)
            if _a_ok:
                _a_desc = _grant_sign_awards(_a_awards)
                _push_granted_items(session, _a_awards)
                save_player_data(PLAYER_DATA)
                session.send(OP_MISSION_SUCCESS, build_mission_success(
                    int(_a_row["id"]), int(_a_row["id"]),
                    build_award_result_proxy("Mission", _a_awards)))
                session.send(OP_DELETE_MISSION, f_int(1, int(_a_row["id"])))
                session.send(OP_UPDATE_CHARACTER, update_character_body())
                # 组内下一个任务（若有）→ 501 NewMission
                mission_achieve.push_updates(session)
                session.log("SubmitMission(%s) 成就任务 → %s" % (_mid, "、".join(_a_desc)))
                return
            # 最后试周期任务（每日击杀/BOSS、每周获得武将、每月合成、三餐体力）
            _p_ok, _p_reason, _p_row, _p_awards = mission_periodic.claim(_mid)
            if _p_ok:
                _p_desc = _grant_sign_awards(_p_awards)
                _push_granted_items(session, _p_awards)
                save_player_data(PLAYER_DATA)
                session.send(OP_MISSION_SUCCESS, build_mission_success(
                    int(_p_row["id"]), int(_p_row["id"]),
                    build_award_result_proxy("Mission", _p_awards)))
                session.send(OP_DELETE_MISSION, f_int(1, int(_p_row["id"])))
                session.send(OP_UPDATE_CHARACTER, update_character_body())
                mission_achieve.push_updates(session)
                session.log("SubmitMission(%s) 周期任务 → %s" % (_mid, "、".join(_p_desc)))
                return
            session.log("SubmitMission(%s) 拒绝：%s / %s" % (_mid, reason, _tok_reason))
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        # ① 领奖结果（客户端弹奖励窗 + 记 completedMissionList）
        session.send(OP_MISSION_SUCCESS, build_mission_success(
            node["id"], node["id"], build_award_result_proxy("Mission", awards)))
        # ② 删掉旧任务（客户端 onCompleteMissionHandler **不会**自己删）
        session.send(OP_DELETE_MISSION, f_int(1, int(node["id"])))
        # ③ 元宝/铜钱变了
        session.send(OP_UPDATE_CHARACTER, update_character_body())
        # ④ 接下一个主线节点 —— ★ 必须用 snapshot 里算好的 complete，
        #    不能写死 0：claim() 内部已按历史通关把下一节点标成可领
        #    （否则下一关明明早通关了，客户端仍显示"未解锁"）。
        _nxt_list, _ = mission.snapshot()
        if _nxt_list:
            session.send(OP_NEW_MISSION, build_mission_data(_nxt_list[0]))
        _nxt = mission.current()
        session.log("SubmitMission(%s) 成功 奖励=%s → 下一个=%s" % (
            _mid, awards, _nxt["id"] if _nxt else "无（主线完成）"))
    elif opcode == OP_BUY_EP:
        # 买体力（10051，无 body）-> 裸 CharacterData
        ok, reason, cost, gain, is_free = buy.buy_ep()
        if not ok:
            session.log("BuyEp 拒绝：%s" % reason)
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        session.send(OP_BUY_EP, build_buy_character_body())
        broadcast(OP_UPDATE_CHARACTER, update_character_body())
        session.log("BuyEp -> 体力+%d（花费 %s，免费=%s，次数=%s）" % (
            gain, cost, is_free, buy_snapshot()))
    elif opcode == OP_BUY_COIN:
        # 买铜钱（10050，无 body）-> 裸 CharacterData（暴击时先推 10053 播特效）
        ok, reason, cost, gained, is_free, crit = buy.buy_coin()
        if not ok:
            session.log("BuyCoin 拒绝：%s" % reason)
            session.send(5002, f_int(1, 0) + f_str(2, reason))
            return
        save_player_data(PLAYER_DATA)
        # ★ 10053 必须**每次**都推，不能只在暴击时推：
        #   客户端 PlayerInfoPanel:coinBaoJiEffect(coin, baoJiType) 里
        #   "获得铜钱%d" 是主干显示，`baoJiType > 1` 才额外贴 x2 倍率图。
        #   只在暴击时推 → 普通购买玩家看不到自己拿了多少铜钱。
        _ci = crit or {"rate": 1, "coin": gained, "rateCoin": gained}
        session.send(OP_COIN_CRIT, build_coin_crit_info(
            _ci["rate"], _ci["coin"], _ci["rateCoin"]))
        session.send(OP_BUY_COIN, build_buy_character_body())
        broadcast(OP_UPDATE_CHARACTER, update_character_body())
        session.log("BuyCoin -> 铜钱+%d（花费 %s，免费=%s，暴击=%s，次数=%s）" % (
            gained, cost, is_free, crit, buy_snapshot()))
    elif opcode == OP_USE_ACTIVATION_CODE:
        # ★ 激活码输入框改造为 GM 命令台（2026-10-22 用户需求）：
        #   客户端设置界面把输入文本以 StringMessage{msg} 发到 8000，本服务端
        #   按 GM 命令解析执行（add_item / add_hero / add_diamond ...）。
        #   结果用 5002 ShowMessage 弹窗回显；【不回 8000】—— 客户端
        #   getExchangeAwardHandler 会把 8000 回包按 AwardResult 解，会错位。
        text = req_string_message(tmsg, pb, _read_string_field)
        result = gm_console_execute(text)
        session.log("GM控制台: %r -> %s" % (text, (result or "").replace("\n", " | ")[:80]))
        session.send(5002, f_int(1, 0) + f_str(2, result))
    elif opcode == 10008:
        session.log("(heartbeat/ping) 未实现，不回")
    else:
        # 通用兜底：对未知 opCode 回「同 opCode + 空 protobuf body」。
        # 目的不是功能正确，而是避免客户端 send() 的 9 秒超时 -> 弹 Alert -> 断线重连/退回登录。
        # 如果某个 opCode 回空包导致客户端崩（日志出 LUA ERROR），再针对性构造数据。
        session.log("(unhandled opCode %d, pb=%d bytes, hex=%s) -> 回空包兜底"
                 % (opcode, len(pb), pb[:16].hex()))
        session.send(opcode, b"")



# ----------------------------------------------------------------------
# HTTP 网关常量（由 mock_server.py 原样移植）
# ★ 2026-10-30：ip/port 从 settings 取（SGHY_PUBLIC_IP/SGHY_TCP_PORT 可覆盖）——
#   手机端连的是局域网 IP，写死 127.0.0.1 会让客户端连到它自己（实测踩过）。
# ----------------------------------------------------------------------
from .. import settings as _settings

SERVER_LIST = {
    "status": True,
    "data": {
        "serverlist": [{
            "serverid": SERVER_ID,
            "servid": SERVER_ID,
            "dispid": 1,
            "name": "本地测试服",
            "ip1": _settings.PUBLIC_IP,
            "ip2": _settings.PUBLIC_IP,
            "dbip": _settings.PUBLIC_IP,
            "game_port": str(_settings.TCP_PORT),
            "open_time": "2026-01-01 00:00:00",
            "difftime": 0,
            "status": 1,
            "mergid": SERVER_ID,
        }],
        "platform": {"vistortype": 1, "showprivate": 2},
    },
}

UPDATE_CONFIG = {
    "status": True,
    "data": {
        "serverip": _settings.PUBLIC_IP,
        "platform": {
            "p_id": "61",
            "p_name": "youguTV",
            "downLoad_url": "http://%s:%d/" % (_settings.PUBLIC_IP, _settings.HTTP_PORT),
            "version": 40469,
        },
    },
}

ANNOUNCE = {
    "status": True,
    "data": [{
        "id": 2,
        "title": "测试一服",
        "content": (
            "邮件HTML地址\n"
            "http://192.168.1.5:8000/mail\n"
        ),
        "starttime": "2026-01-01 00:00:00",
        "endtime": "2030-01-01 00:00:00",
    }],
}


