"""配置表解析器：从反编译的 Lua 配置中提取游戏数据。
服务端 growth / 战斗 / 活动等模块统一从这里读配置。
"""
import re, os, json, time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))   # 仓库根（.../HYSG）
_CONFIG_DIR = os.environ.get(
    "SGHY_CONFIG_DIR", os.path.join(_ROOT, "env", "_work", "rt_all"))

# 配置解析缓存：_parse_lua_table 每次要读 1~6 MB 的 Lua 文本，
# 而 get_hero_name() 这类访问器会被逐个武将调用上百次。
# 没有缓存时序列化 96 个 HeroData 要 7 秒（等于登录卡 7 秒），加缓存后 ~0ms。
_CACHE = {}
_CACHE_STAT_INTERVAL = 1.0      # 秒：mtime 检查节流（Windows 上 stat 一次约 0.5~0.8 ms）
_last_stat = {}


def _cached_parse(path):
    """按路径缓存解析结果。文件 mtime 变了会重新解析（节流检查，改配置无需重启）。

    注意：不要在每次访问时都 stat —— 访问器被上百次调用，stat 的开销会盖过解析本身。
    """
    now = time.monotonic()
    hit = _CACHE.get(path)
    if hit and now - _last_stat.get(path, 0.0) < _CACHE_STAT_INTERVAL:
        return hit[1]
    _last_stat[path] = now
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0
    if hit and hit[0] == mt:
        return hit[1]
    data = _parse_lua_table(path)
    _CACHE[path] = (mt, data)
    return data


def clear_cache():
    _CACHE.clear()
    _FLAG_CACHE.clear()
    _last_stat.clear()


def reload_all():
    """配置热重载：清各模块缓存并重新预热。

    用法：GET /api/gm?action=reload_config
    注意：玩家已保存的武将属性在下次登录时按新配置重算；
          已在线角色看到的数值要重新登录才刷新。
    """
    from . import challenge, collect_config, growth, lottery_config
    from .activities import common as act_common

    clear_cache()
    for mod in (lottery_config, collect_config):
        fn = getattr(mod, "clear_cache", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
    act_common._CONFIG_CACHE = None
    challenge._INDEX = None
    challenge._CI_CACHE = {"mtime": None, "rows": None}
    ok = False
    try:
        ok = bool(growth.reset())
    except Exception as e:
        return "成长表重载失败: %r" % e
    heroes = load_hero_config()
    levels = load_level_set_config()
    return "配置已重载（成长表=%s, 武将=%d, 关卡布阵=%d）；玩家需重新登录刷新数值" % (
        "ok" if ok else "warn", len(heroes), len(levels))

def _parse_lua_table(filepath, index_field="ID"):
    """通用 Lua table 解析器：提取 [{...}] 条目为 dict 列表。"""
    results = {}
    with open(filepath, encoding="utf-8") as f:
        lines = f.readlines()
    in_entry = False
    current_id = 0
    fields = {}
    for line in lines:
        s = line.strip()
        m = re.match(r'\[(\d+)\]\s*=\s*\{', s)
        if m:
            if current_id and fields:
                results[current_id] = fields
            current_id = int(m.group(1))
            fields = {"_id": current_id}
            in_entry = True
        elif in_entry:
            if s in ('},', '}'):
                if current_id and fields:
                    results[current_id] = fields
                fields = {}
                in_entry = False
                continue
            if s.startswith('['):
                if current_id and fields:
                    results[current_id] = fields
                m2 = re.match(r'\[(\d+)\]', s)
                current_id = int(m2.group(1)) if m2 else current_id
                fields = {"_id": current_id}
                continue
            if '=' in s:
                parts = s.split('=', 1)
                key = parts[0].strip()
                val = parts[1].strip().rstrip(',')
                fields[key] = val
    return results


def load_honor_config():
    """加载 HonorConfig（玩家头像/头像框），返回 {id: {HonorPart, Icon, Name, ...}}

    HonorPart = 0 → 头像（userHead/*.jpg）
    HonorPart = 1 → 头像框（headFrame/border_*.png）
    """
    path = os.path.join(_CONFIG_DIR, "app.config.HonorConfig.lua")
    return _cached_parse(path)


def honor_ids():
    """(头像ID列表, 头像框ID列表)"""
    cfg = load_honor_config()

    def _i(v, d=0):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return d

    heads, frames = [], []
    for hid, row in (cfg or {}).items():
        (frames if _i(row.get("HonorPart")) == 1 else heads).append(hid)
    return sorted(heads), sorted(frames)


def load_hero_config():
    """加载 HeroConfig，返回 {id: {BaseHP, BaseAttack, ...}}。

    ★ 2026-11-01 改：用嵌套解析器。扁平解析器遇到多行嵌套表（CanRelation）
      会提前收尾，丢掉其后的 CanBreakLevel / IsDevil / ShowInCollect 等字段
      （配置表由 tools/config_tool.py 统一生成，格式与嵌套深度不再固定）。
    """
    path = os.path.join(_CONFIG_DIR, "app.config.new.HeroConfig.lua")
    data = _load_lua_full(path)
    if isinstance(data, dict) and data:
        return data
    return _cached_parse(path)


def load_avatar_config():
    """加载 AvatarConfig，返回 {id: {Spine, Avatar, Face, HangingPoint, ...}}"""
    path = os.path.join(_CONFIG_DIR, "app.config.new.AvatarConfig.lua")
    return _cached_parse(path)


def load_level_set_config():
    """加载 LevelSetConfig，返回 {entry_id: {Type, Level, Wave, Position, RealHp, ...}}

    ★ 2026-11-01 改：用嵌套解析器（与 HeroConfig 同理）。条目键顺序必须保持
      基线顺序——服务端解析器会把“连续 1..n 的顶层键”折叠成 list；
      tools/config_tool.py 的生成器按 CSV 行顺序输出，不会重排。
    """
    path = os.path.join(_CONFIG_DIR, "app.config.new.LevelSetConfig.lua")
    data = _load_lua_full(path)
    if isinstance(data, dict) and len(data) > 100:
        return data
    return _cached_parse(path)


def load_level_config():
    """加载 LevelConfig（关卡主表），返回 {level_id: {...}}。

    ★ 必须用嵌套解析器：LevelConfig 每条里带 `DayOfWeeks = {...}` / `LevelEnemy = {...}`
    等嵌套表，旧的扁平解析器 `_cached_parse` 遇到嵌套的 `}` 就提前收尾，
    导致 `SceneType`(章节) / `IsBoss` / `IsFinalLevel` / `LevelType` / `LastLevel`
    这些**在后面**的字段全部丢失（2026-10-24 实现购买体力时发现）。
    这些字段正是「通关每章送免费购买次数」的依据。

    关键字段：
      SceneType    章节号（对应 SceneTypeConfig 的 ID）
      LastLevel    上一关（解锁链）
      IsFinalLevel 1 = 本章最后一关
      IsBoss       1 = BOSS 关
      LevelType    1 = 普通 / 2 = 精英（超级）
      Ep           体力消耗
    """
    path = os.path.join(_CONFIG_DIR, "app.config.new.LevelConfig.lua")
    if not os.path.exists(path):
        path = os.path.join(_CONFIG_DIR, "app.config.LevelConfig.lua")
    return _load_lua_full(path)


def load_sign_award_config():
    """加载 SignAwardConfig（签到奖励，ID=日期 YYYYMMDD）。

    条目：{ID, VIPLv（VIP双倍门槛，0=无双倍）, ExtraTimes（额外领取次数）,
          Awards: [{ClassName, Type, Count}]}
    注：含嵌套 Awards 表，用专用正则解析（_cached_parse 是扁平的）。
    """
    import re as _re
    path = os.path.join(_CONFIG_DIR, "app.config.new.SignAwardConfig.lua")
    src = _read_cached(path)
    out = {}
    heads = [(m.start(), m.group(1)) for m in _re.finditer(r'\[\s*(\d+)\s*\]\s*=\s*\{', src)]
    heads.append((len(src), None))
    for (s, hid), (e, _) in zip(heads, heads[1:]):
        if not hid:
            continue
        body = src[s:e]
        def num(key):
            m = _re.search(r'\b' + key + r'\s*=\s*(-?\d+)', body)
            return int(m.group(1)) if m else 0
        awards = []
        # 两类奖励：ObjectAward{Type,Count}=道具 / PropertyAward{PropertyName,Value}=属性（元宝等）
        for am in _re.finditer(
                r'\{[^{}]*?ClassName\s*=\s*"(ObjectAward|PropertyAward)"[^{}]*?\}', body):
            ab = am.group(0)
            if 'ObjectAward' in ab:
                tm = _re.search(r'\bType\s*=\s*(\d+)', ab)
                cm = _re.search(r'\bCount\s*=\s*(\d+)', ab)
                if tm:
                    awards.append({"kind": "object", "type": int(tm.group(1)),
                                   "count": int(cm.group(1)) if cm else 0})
            else:
                pm = _re.search(r'\bPropertyName\s*=\s*"([^"]+)"', ab)
                vm = _re.search(r'\bValue\s*=\s*(-?\d+)', ab)
                if pm:
                    awards.append({"kind": "property", "prop": pm.group(1),
                                   "value": int(vm.group(1)) if vm else 0})
        out[int(hid)] = {
            "id": int(hid),
            "vip_lv": num("VIPLv"),
            "extra_times": num("ExtraTimes"),
            "awards": awards,
        }
    return out


def get_month_sign_awards(year, month):
    """按年月切出当月签到奖励列表（对齐客户端 SignManager:getCurMonthAwardList）。

    SignAwardConfig 的 ID 是 YYYYMMDD；当月列表 = ID ∈ [YYYYMM01, YYYYMM31]，
    按 ID 升序（= 日期顺序）。返回 [{id, vip_lv, extra_times, awards}, ...]
    """
    cfg = load_sign_award_config()
    lo = int("%04d%02d01" % (year, month))
    hi = int("%04d%02d31" % (year, month))
    items = [v for k, v in cfg.items() if lo <= v["id"] <= hi]
    items.sort(key=lambda v: v["id"])
    return items


def _read_cached(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ---------------------------------------------------------------- 军旗配置
# （2026-10-23）FlagPart/FlagSuit/FlagDraw/FlagDrawLevel/FlagPartSpecialSynthetic
# 五张表 + GameSetConfig 均含**嵌套表**（ProductCost/Addition/DrawStageInfo...），
# _parse_lua_table 只支持扁平行，这里给一个通用递归 Lua table 解析器。
# 反编译产物格式规整（无注释/无长括号字符串），够用且零依赖。

_LUA_WS = " \t\r\n"

# ★ 2026-10-29 性能修：原实现每解析一个值都 `re.match(pat, s[i:])`——切整条剩余
#   字符串，1.6MB 的 LevelConfig 累计 O(n²) 复制，首次加载实测 **12.7 秒**（表现为
#   「打完关卡结算界面要等十几秒」，因为 LevelEnd 里才首次 load_level_config）。
#   改用带 pos 的 compiled.match(s, i)，零切片。
_RE_LUA_NUM = re.compile(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?")
_RE_LUA_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*=")
# 跳过空白/逗号/分号（解析器里这些位置都是「跳过」语义，批量跳过比逐字符快一个量级）
_RE_LUA_SKIP = re.compile(r"[ \t\r\n,;]*")
_RE_LUA_WS_SKIP = re.compile(r"[ \t\r\n]*")


def _lua_skip_ws(s, i):
    i = _RE_LUA_WS_SKIP.match(s, i).end()
    return i


def _lua_string(s, i):
    r"""i 指向开引号，返回 (解码后的值, 下一位置)。支持 \ddd 十进制与常见转义。"""
    q = s[i]
    i += 1
    out = []
    while i < len(s):
        c = s[i]
        if c == q:
            return _decode_lua_bytes(out), i + 1
        if c == "\\":
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if nxt.isdigit():
                j = i + 1
                digits = ""
                while j < len(s) and s[j].isdigit() and len(digits) < 3:
                    digits += s[j]
                    j += 1
                try:
                    # ★ 2026-10-28 修：lua \ddd 是【十进制】转义（原写成 8 进制，
                    #   所有经 _load_lua_full 取出的 Name 等字符串都是乱码，
                    #   如碎片合成提示里的装备名）。get_hero_name 一直用十进制是对的。
                    out.append(chr(int(digits, 10)))
                except ValueError:
                    pass
                i = j
                continue
            mp = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'",
                  "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
            out.append(mp.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return _decode_lua_bytes(out), i


def _decode_lua_bytes(out):
    r"""把 _lua_string 攒出的字符列表还原成 UTF-8 字符串。

    \ddd 十进制转义贡献的是单个字节（0-255），要与字面 ASCII 混拼后
    整体做 latin-1 -> utf-8 还原；失败（含 >255 字符）就原样返回。
    """
    s = "".join(out)
    if not any(ord(ch) > 127 for ch in s):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _lua_value(s, i):
    """解析从 i 开始的一个 Lua 值，返回 (value, 下一位置)。value: dict/list/str/int/float/bool/None"""
    i = _lua_skip_ws(s, i)
    if i >= len(s):
        return None, i
    c = s[i]
    if c == "{":
        return _lua_table(s, i)
    if c in "\"'":
        v, i = _lua_string(s, i)
        return v, i
    if s.startswith("true", i):
        return True, i + 4
    if s.startswith("false", i):
        return False, i + 5
    if s.startswith("nil", i):
        return None, i + 3
    m = _RE_LUA_NUM.match(s, i)
    if m:
        txt = m.group(0)
        return (float(txt) if ("." in txt or "e" in txt or "E" in txt)
                else int(txt)), m.end()
    # 兜底：读到下一个分隔符当裸词
    j = i
    while j < len(s) and s[j] not in ",};" + _LUA_WS:
        j += 1
    return s[i:j], j


def _lua_table(s, i):
    """i 指向 '{'。返回 (dict 或 list, 下一位置)。
    连续 1..n 的纯数组键折叠成 list，其余保持 {int|str: value}。"""
    assert s[i] == "{"
    i += 1
    entries = []          # (key or None, value)
    arr_auto = 0          # 隐式数组键的下一个下标（Lua 1 基）
    while True:
        i = _lua_skip_ws(s, i)
        if i >= len(s):
            break
        if s[i] == "}":
            i += 1
            break
        key = None
        if s[i] == "[":
            key, i = _lua_value(s, i + 1)
            i = _lua_skip_ws(s, i)
            if i < len(s) and s[i] == "]":     # 消费闭括号（漏掉会把 ] 当裸词值）
                i += 1
            i = _lua_skip_ws(s, i)
            if i < len(s) and s[i] == "=":
                i += 1
        else:
            m = _RE_LUA_KEY.match(s, i)
            if m and s[i - 1:i] != "]":
                key = m.group(0).split("=")[0].strip()
                i = m.end()          # ★ 绝对位置（带 pos 匹配），不能再 i +=
            else:
                key = arr_auto + 1
        # key 后可能还有空格（上面 re 已吃掉 = 前空格）
        val, i = _lua_value(s, i)
        if key == arr_auto + 1:
            arr_auto += 1
        entries.append((key, val))
        i = _lua_skip_ws(s, i)
        if i < len(s) and s[i] in ",;":
            i += 1
    # 折叠纯数组
    if entries and all(k == n + 1 for n, (k, _) in enumerate(entries)):
        return [v for _, v in entries], i
    out = {}
    for k, v in entries:
        out[k] = v
    return out, i


_FLAG_CACHE = {}


def _load_lua_full(path):
    """通用嵌套 Lua 配置加载（带 mtime 缓存）。返回首个顶层 table。"""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _FLAG_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    src = _read_cached(path)
    i = _lua_skip_ws(src, 0)
    # 跳到第一个 '{'（跳过 local data =）
    while i < len(src) and src[i] != "{":
        i += 1
    data, _ = _lua_table(src, i)
    _FLAG_CACHE[path] = (mt, data)
    return data


def load_game_set_config():
    """GameSetConfig[1]：MaxFlagCount/UnlockFlagCost/DivinationNeedCoin 等全局参数。
    顶层 { [1]={...} } 会被折叠成单元素 list，两种形状都兼容。"""
    data = _load_lua_full(os.path.join(_CONFIG_DIR, "app.config.GameSetConfig.lua"))
    if isinstance(data, list):
        data = data[0] if data else {}
    return data or {}


def load_flag_part_config():
    """FlagPartConfig {type: {Part, Group, Quality, ProductCost[], UpCost[],
    UnlockCost[], Addition[{IndexID,Type,MaxValue,Mode}], AdditionCount, ...}}"""
    return _load_lua_full(os.path.join(_CONFIG_DIR, "app.config.FlagPartConfig.lua"))


def load_exp_set_config():
    """ExpSetConfig，按 **Lv** 索引（1..80）。

    用到的字段：
      MaxEp            体力上限（客户端 User:getMaxEp 直接读这一列）
      LvUpEp           队伍升级时补的体力
      TeamExp          升到下一级所需队伍经验
      ResetLevelDollar 重置关卡扫荡次数价格（客户端按 buyTimes+1 查 Lv 行）
      BuyEp            买体力的元宝价（按 buyEpTimes+1 查 Lv 行）
    """
    data = _load_lua_full(os.path.join(_CONFIG_DIR, "app.config.new.ExpSetConfig.lua"))
    out = {}
    rows = data.items() if isinstance(data, dict) else enumerate(data or [], 1)
    for k, v in rows:
        if not isinstance(v, dict):
            continue
        try:
            out[int(v.get("Lv") or k)] = v
        except (TypeError, ValueError):
            continue
    return out


def load_vip_config():
    """VIPSetConfig，按 **Lv**（0..N）索引。

    扫荡相关：ReapLevelTimes（每日可领扫荡令牌数）、BuyLevelTimes（每日可重置次数）。
    """
    data = _load_lua_full(os.path.join(_CONFIG_DIR, "app.config.new.VIPSetConfig.lua"))
    out = {}
    rows = data.items() if isinstance(data, dict) else enumerate(data or [], 1)
    for k, v in rows:
        if not isinstance(v, dict):
            continue
        lv = v.get("Lv")
        try:
            # ★ Lv=0 是合法值（VIP0），不能用 `or` 兜底（0 会被当假值 → 键错成 1）
            out[int(k if lv is None else lv)] = v
        except (TypeError, ValueError):
            continue
    return out


def load_flag_suit_config():
    """FlagSuitConfig {group: {Count[], SkillType[][], SkillTypeNew[][], AddPowerPer[]}}"""
    return _load_lua_full(os.path.join(_CONFIG_DIR, "app.config.FlagSuitConfig.lua"))


# 套装名 → 势力（HeroConfig.Origin，OriginType: 魏1/蜀2/吴3/群4）。
# 用户 2026-10-27 拍板：带势力的套装（如蜀汉）效果只对该势力的上阵武将生效；
# 无势力关键词的套装（战鼓/贪婪/烈火燎原等）对编队全体生效。
_FLAG_SUIT_ORIGIN_KEYWORDS = (("蜀汉", 2), ("曹魏", 1), ("东吴", 3), ("群雄", 4))

_FLAG_SUIT_ORIGIN_CACHE = None


def flag_suit_origin(group):
    """FlagSuitConfig 套装的势力限定（HeroConfig.Origin 值）。

    按套装 Name 关键词判定（蜀汉/曹魏/东吴/群雄）；无势力关键词返回 None
    （表示对全体武将生效）。Name 是 \\ddd 十进制转义，用 decode_lua_str 解。
    """
    global _FLAG_SUIT_ORIGIN_CACHE
    if _FLAG_SUIT_ORIGIN_CACHE is None:
        cache = {}
        try:
            raw = _read_cached(os.path.join(_CONFIG_DIR, "app.config.new.FlagSuitConfig.lua"))
            for m in re.finditer(r'\n  \[(\d+)\] = \{', raw):
                nxt = re.search(r'\n  \[\d+\] = \{', raw[m.end():])
                body = raw[m.start(): m.end() + nxt.start() if nxt else len(raw)]
                nm = re.search(r'Name = "((?:[^"\\]|\\.)*)"', body)
                if not nm:
                    continue
                name = decode_lua_str(nm.group(1)) or ""
                origin = None
                for kw, ov in _FLAG_SUIT_ORIGIN_KEYWORDS:
                    if kw in name:
                        origin = ov
                        break
                cache[int(m.group(1))] = origin
        except Exception:
            pass
        _FLAG_SUIT_ORIGIN_CACHE = cache
    return _FLAG_SUIT_ORIGIN_CACHE.get(int(group or 0))


def load_flag_draw_config():
    """FlagDrawConfig {map_id: {NeedLv, DivinationNeedCoin, Exp, DrawStageInfo[], ...}}"""
    return _load_lua_full(os.path.join(_CONFIG_DIR, "app.config.FlagDrawConfig.lua"))


def load_flag_draw_level_config():
    """FlagDrawLevelConfig {id: {Lv, NeedExp, Probability, TempBagCapacity}}"""
    return _load_lua_full(os.path.join(_CONFIG_DIR, "app.config.FlagDrawLevelConfig.lua"))


def load_flag_synthetic_config():
    """FlagPartSpecialSyntheticConfig {id: {Cost[{Condition,Value}], Awards[], ...}}"""
    return _load_lua_full(os.path.join(_CONFIG_DIR, "app.config.FlagPartSpecialSyntheticConfig.lua"))


def flag_part_cfg(part_type):
    return load_flag_part_config().get(int(part_type))


def ensign_game_set(key, default=0):
    try:
        v = load_game_set_config().get(key)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _toi(v, d=0):
    """配置值转 int（_parse_lua_table 取到的是字符串，可能是 "250" 或 "250.0"）。"""
    try:
        if v is None or v == "":
            return d
        return int(float(v))
    except (TypeError, ValueError):
        return d


def get_level_enemies(level_id, level_set_config=None):
    """获取某关卡的所有敌人，按 Wave/Position 排序。

    LevelSetConfig 列语义（已核对）：
      Real*      基础真实值（客户端 LevelWarriorData 也读这三列显示）
      Scale      强度倍率（样本 100 / 120），真服会按它缩放
      GradeLevel 档位等级
      HP/Attack/Defence  修正项，可为负，语义待核实（暂不参与计算）
    返回: [{type, wave, position, hp, attack, defence, ..., scale, grade_level}]
    """
    if level_set_config is None:
        level_set_config = load_level_set_config()
    enemies = []
    for eid, entry in level_set_config.items():
        if str(entry.get("Level")) == str(level_id):
            enemies.append({
                "entry_id": eid,
                "type": _toi(entry.get("Type"), 15011),
                "wave": _toi(entry.get("Wave"), 1),
                "position": _toi(entry.get("Position"), 1),
                "hp": _toi(entry.get("RealHp"), 110),
                "attack": _toi(entry.get("RealAtk"), 90),
                "defence": _toi(entry.get("RealDef"), 10),
                "crit": _toi(entry.get("RealCrit"), 10),
                "combo": _toi(entry.get("RealCombo"), 0),
                "block": _toi(entry.get("RealBlock"), 0),
                "counter": _toi(entry.get("RealCounter"), 8),
                "crit_damage": _toi(entry.get("RealCritDamage"), 50),
                "tenacity": _toi(entry.get("RealTenacity"), 0),
                "hit": _toi(entry.get("RealHit"), 0),
                "miss": _toi(entry.get("RealMiss"), 0),
                "is_boss": _toi(entry.get("IsBoss"), 0),
                "scale": _toi(entry.get("Scale"), 100),
                "grade_level": _toi(entry.get("GradeLevel"), 0),
                # ShowLevel = 敌人头顶显示等级（配置 1~80 随关卡递增，1001→1、1005→3、90064→80）
                "show_level": _toi(entry.get("ShowLevel"), 1),
                # 增减伤 / 抗性 / 命中——HeroData 有对应字段，原版会下发
                "damage_increase": _toi(entry.get("RealDamageIncrease"), 0),
                "damage_decrease": _toi(entry.get("RealDamageDecrease"), 0),
                "is_devil": _toi(entry.get("IsDevil"), 0),
            })
    enemies.sort(key=lambda e: (e["wave"], e["position"]))
    return enemies


def get_hero_stats(hero_id, level=1):
    """获取武将属性数值（按 HeroConfig 基础值 + 等级成长计算）。
    ExpSetConfig 级别 1 的 LvGrowth 全是 0，所以基础值 = 最终值。
    """
    hero_config = load_hero_config()
    hc = hero_config.get(hero_id)
    if not hc:
        return {"maxHP": 295, "attack": 99, "defence": 51, "speed": 12}
    base_hp = int(hc.get("BaseHP", 295))
    base_atk = int(hc.get("BaseAttack", 99))
    base_def = int(hc.get("BaseDefence", 51))
    base_spd = int(hc.get("BaseSpeed", 12))
    hp_mod = int(hc.get("HPMod", 0))
    atk_mod = int(hc.get("AttackMod", 0))
    def_mod = int(hc.get("DefenceMod", 0))
    return {
        "maxHP": base_hp + hp_mod,
        "attack": base_atk + atk_mod,
        "defence": base_def + def_mod,
        "speed": base_spd,
    }


def decode_lua_str(s):
    r"""解码 Lua 字符串里的 `\ddd` 转义（**十进制**，不是八进制）。

    例：`"\230\130\172"` → 230,130,172 = 0xE6 0x82 0xAC = "悬"。
    """
    if not s:
        return ""
    out = bytearray()
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and s[i + 1].isdigit():
            j = i + 1
            digits = ""
            while j < n and s[j].isdigit() and len(digits) < 3:
                digits += s[j]
                j += 1
            try:
                out.append(int(digits, 10) & 0xFF)
            except ValueError:
                pass
            i = j
        else:
            out.extend(ch.encode("utf-8"))
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return out.decode("utf-8", "replace")


_ITEM_NAME_CACHE = {}


def item_name(item_id):
    """道具名字（带缓存）。管理端道具选择器要用。"""
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return ""
    hit = _ITEM_NAME_CACHE.get(iid)
    if hit is not None:
        return hit
    name = "道具%d" % iid
    try:
        path = os.path.join(_CONFIG_DIR, "app.config.new.ItemConfig.lua")
        src = _read_cached(path)
        m = re.search(r"\[\s*%d\s*\]\s*=\s*\{" % iid, src)
        if m:
            seg = src[m.end():m.end() + 2500]
            nm = re.search(r'Name\s*=\s*"((?:\\\d{1,3}|[^"\\])*)"', seg)
            if nm:
                name = decode_lua_str(nm.group(1)) or name
    except Exception:
        pass
    _ITEM_NAME_CACHE[iid] = name
    return name


_HERO_NAME_CACHE = {}


def get_hero_name(hero_id):
    r"""获取武将名字（兼容转义与明文两种配置格式）—— 带缓存。

    EnterGame 要给 512 个武将各取一次名，每次正则+解码约 2µs；
    名字是静态配置，缓存不失效（配置热更也不会改武将名）。

    ★ 2026-11-01 修：老配置里 Name 是 `\229\143\x` 形式的 UTF-8 字节转义，
    新配置（app.config.new.hero）已经是**明文中文**。旧实现只认转义，明文一律
    回退成"英雄" —— 结果 1468 个武将全叫「英雄」，发信台搜"诸葛亮"永远搜不到
    （286 个 4~6★ 全是同名，6★ 诸葛亮 10186 也在里面只是看不见）。
    """
    hit = _HERO_NAME_CACHE.get(hero_id)
    if hit is not None:
        return hit
    hero_config = load_hero_config()
    hc = hero_config.get(hero_id, {})
    raw = hc.get("Name", "")
    name = "英雄"
    if raw:
        bytes_list = re.findall(r'\\(\d+)', raw)
        if bytes_list:
            try:
                name = bytes(int(b) for b in bytes_list if int(b) <= 255).decode("utf-8")
            except Exception:
                name = str(raw).strip() or "英雄"
        else:
            name = str(raw).strip() or "英雄"
    _HERO_NAME_CACHE[hero_id] = name
    return name


def get_hero_avatar(hero_id):
    """获取武将的 spine 形象配置（通过 HeroConfig.AvatarType → AvatarConfig）。"""
    hero_config = load_hero_config()
    avatar_config = load_avatar_config()
    hc = hero_config.get(hero_id, {})
    avatar_type = int(hc.get("AvatarType", hero_id))
    ac = avatar_config.get(avatar_type, {})
    return {
        "avatar_type": avatar_type,
        "spine": ac.get("Spine", ""),
        "avatar": ac.get("Avatar", ""),
        "face": ac.get("Face", ""),
        "zoom": int(ac.get("zoom", 100)),
        "is_melee": int(ac.get("IsMelee", 1)),
        "hanging_point": {
            "head": int(ac.get("HangingPoint.head", 170)) if isinstance(ac.get("HangingPoint.head"), str) else 170,
            "face": 116, "body": 76  # 简化
        }
    }


def get_hero_star_info(hero_id):
    """获取武将星级链信息：{quality, next_type, max_star_type, avatar_type}"""
    hero_config = load_hero_config()
    hc = hero_config.get(hero_id, {})
    return {
        "quality": int(hc.get("Quality", 1)),
        "next_type": int(hc.get("NextType", 0)),
        "max_star_type": int(hc.get("maxStarType", hero_id)),
        "avatar_type": int(hc.get("AvatarType", hero_id)),
    }


if __name__ == "__main__":
    # 测试
    enemies = get_level_enemies(1002)
    print("Level 1002 enemies:", len(enemies))
    for e in enemies:
        print(f"  Type={e['type']} Wave={e['wave']} Pos={e['position']} HP={e['hp']} Atk={e['attack']}")
    stats = get_hero_stats(10121)
    print("Guan Yu (10121) stats:", stats)
    print("Guan Yu name:", get_hero_name(10121))

def _decode_escapes(s):
    """把 Lua 字符串里的 \\ddd（十进制字节）转义还原成 UTF-8 文本。"""
    if not s:
        return ""
    s = str(s).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    out = bytearray()
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n:
            j = i + 1
            if s[j].isdigit():
                k = j
                while k < n and s[k].isdigit() and k - j < 3:
                    k += 1
                try:
                    out.append(int(s[j:k]) & 0xFF)
                except ValueError:
                    pass
                i = k
                continue
            mapping = {'n': b'\n', 't': b'\t', 'r': b'\r', '"': b'"', "'": b"'", '\\': b'\\'}
            out.extend(mapping.get(s[j], s[j].encode('utf-8')))
            i += 2
            continue
        out.extend(c.encode('utf-8'))
        i += 1
    return out.decode('utf-8', 'replace')


def _item_config():
    return _cached_parse(os.path.join(_CONFIG_DIR, "app.config.ItemConfig.lua"))


def _equip_config():
    return _cached_parse(os.path.join(_CONFIG_DIR, "app.config.EquipConfig.lua"))


def item_exists(item_type):
    try:
        return int(item_type) in _item_config()
    except (TypeError, ValueError):
        return False


def get_item_name(item_type):
    """物品名（解码 \\ddd 转义）；查不到回退 '物品N'。"""
    try:
        row = _item_config().get(int(item_type)) or {}
        name = _decode_escapes(row.get("Name"))
        if name:
            return name
    except Exception:
        pass
    return "物品%d" % int(item_type)


def get_equip_name(equip_type):
    """装备名（EquipConfig.Name 解码）；查不到回退 '装备N'。"""
    try:
        row = _equip_config().get(int(equip_type)) or {}
        name = _decode_escapes(row.get("Name"))
        if name:
            return name
    except Exception:
        pass
    return "装备%d" % int(equip_type)


def get_honor_name(honor_id):
    """头像/头像框名（HonorConfig.Name 解码）；查不到回退 '头像框N'。"""
    try:
        row = load_honor_config().get(int(honor_id)) or {}
        name = _decode_escapes(row.get("Name"))
        if name:
            return name
    except Exception:
        pass
    return "头像框%d" % int(honor_id)

def get_vip_price(vip_lv):
    """VIPSetConfig[Lv].Price = 该档累计充值门槛（客户端 VIP 进度条的区间端点）。"""
    import re as _re
    try:
        path = os.path.join(_CONFIG_DIR, "app.config.new.VIPSetConfig.lua")
        src = _read_cached(path)
        m = _re.search(r'\bLv\s*=\s*%d\b.*?\bPrice\s*=\s*(\d+)' % int(vip_lv), src, _re.S)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0

def load_shop_config():
    """ShopConfig：{id: {Group(4=充值档/3=月卡周卡), Price, Currency, Awards,
    FirstAwards, Name, Duration, MailType, DayAwards}}。"""
    import re as _re
    path = os.path.join(_CONFIG_DIR, "app.config.new.ShopConfig.lua")
    src = _read_cached(path)
    out = {}
    heads = [(m.start(), m.group(1)) for m in _re.finditer(r'\[\s*(\d+)\s*\]\s*=\s*\{', src)]
    heads.append((len(src), None))
    for (st, hid), (e, _) in zip(heads, heads[1:]):
        body = src[st:e]
        def num(key):
            mm = _re.search(r'\b' + key + r'\s*=\s*(-?\d+)', body)
            return int(mm.group(1)) if mm else 0
        def str_of(key):
            """Lua 字符串字段（Name 等，\\ddd 十进制字节转义 → UTF-8）。"""
            mm = _re.search(r'\b' + key + r'\s*=\s*("(?:[^"\\]|\\.)*")', body)
            return _decode_escapes(mm.group(1)) if mm else ""
        def awards_of(key):
            # 抓 Awards/FirstAwards/DayAwards 块里的 {PropertyName=.., Value=..} 或 {Type=.., Count=..}
            mm = _re.search(r'\b' + key + r'\s*=\s*\{', body)
            if not mm:
                return []
            blk = body[mm.end():]
            blk = blk[:blk.find("\n    }")] if "\n    }" in blk else blk[:600]
            res = []
            for am in _re.finditer(r'\{[^{}]*?\}', blk):
                ab = am.group(0)
                pn = _re.search(r'\bPropertyName\s*=\s*"([^"]+)"', ab)
                pv = _re.search(r'\bValue\s*=\s*(\d+)', ab)
                if pn:
                    res.append({"kind": "property", "prop": pn.group(1),
                                "value": int(pv.group(1)) if pv else 0})
                    continue
                tt = _re.search(r'\bType\s*=\s*(\d+)', ab)
                cc = _re.search(r'\bCount\s*=\s*(\d+)', ab)
                if tt:
                    res.append({"kind": "object", "type": int(tt.group(1)),
                                "count": int(cc.group(1)) if cc else 0})
            return res
        out[int(hid)] = {
            "id": int(hid), "group": num("Group"), "price": num("Price"),
            "currency": num("Currency"), "awards": awards_of("Awards"),
            "first_awards": awards_of("FirstAwards"),
            # 月卡/周卡（Group=3）：名称/有效天数/每日奖励/邮件类型
            "name": str_of("Name"), "duration": num("Duration"),
            "mail_type": num("MailType"), "day_awards": awards_of("DayAwards"),
        }
    return out


def get_shop_item(item_id):
    return load_shop_config().get(int(item_id))


def get_vip_max_lv():
    """VIP 最大等级（VIPSetConfig 最大 Lv）。"""
    import re as _re
    try:
        path = os.path.join(_CONFIG_DIR, "app.config.new.VIPSetConfig.lua")
        src = _read_cached(path)
        lvs = [int(m) for m in _re.findall(r'\bLv\s*=\s*(\d+)', src)]
        return max(lvs) if lvs else 18
    except Exception:
        return 18

class _MissionCache(object):
    path = ""
    mtime = -1.0
    data = None
    main = None


def load_mission_config():
    """MissionConfig：{id: {show_position, trigger_value, target, target_value, awards}}。

    ShowPosition=4 = 等级奖励活动（operateActivities.lvAward 消费它）；
    TriggerValue = 等级门槛；Target=-1 无计数目标。

    ★ 必须按 mtime 缓存：1099 条目正则解析一次要 ~1s，而主线任务每次
    snapshot/claim 都要用（147 次领奖不加缓存跑 2 分 49 秒）。
    """
    import re as _re
    path = os.path.join(_CONFIG_DIR, "app.config.new.MissionConfig.lua")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0
    if _MissionCache.data is not None and _MissionCache.path == path and _MissionCache.mtime == mt:
        return _MissionCache.data
    src = _read_cached(path)
    out = {}
    heads = [(m.start(), m.group(1)) for m in _re.finditer(r'\[\s*(\d+)\s*\]\s*=\s*\{', src)]
    heads.append((len(src), None))
    for (st, hid), (e, _) in zip(heads, heads[1:]):
        body = src[st:e]
        def num(key):
            mm = _re.search(r'\b' + key + r'\s*=\s*(-?\d+)', body)
            return int(mm.group(1)) if mm else 0
        # Awards 块（同 ShopConfig 的两种奖励形态）
        awards = []
        mm = _re.search(r'\bAwards\s*=\s*\{', body)
        if mm:
            blk = body[mm.end():]
            blk = blk[:blk.find("\n    }")] if "\n    }" in blk else blk[:800]
            for am in _re.finditer(r'\{[^{}]*?\}', blk):
                ab = am.group(0)
                pn = _re.search(r'\bPropertyName\s*=\s*"([^"]+)"', ab)
                pv = _re.search(r'\bValue\s*=\s*(\d+)', ab)
                if pn:
                    awards.append({"kind": "property", "prop": pn.group(1),
                                   "value": int(pv.group(1)) if pv else 0})
                    continue
                tt = _re.search(r'\bType\s*=\s*(\d+)', ab)
                cc = _re.search(r'\bCount\s*=\s*(\d+)', ab)
                if tt:
                    awards.append({"kind": "object", "type": int(tt.group(1)),
                                   "count": int(cc.group(1)) if cc else 0})
        out[int(hid)] = {
            "id": int(hid), "show_position": num("ShowPosition"),
            "trigger_value": num("TriggerValue"), "target": num("Target"),
            "target_value": num("TargetValue"), "awards": awards,
            # 主线任务（2026-10-25）：MainIndex>0 即主线节点
            "main_index": num("MainIndex"),
            "next": num("Next"),
            "target_id": num("TargetID"),
            "cycle": num("Cycle"),
            "team_exp": num("TeamExp"),
            "min_lv": num("MinLv"), "max_lv": num("MaxLv") or 99,
            "min_vip": num("MinVIP"), "max_vip": num("MaxVIP") or 99,
            # 周期任务（2026-10-29）：Period=刷新秒数/三餐闭窗(HHMM)
            "period": num("Period"),
        }
    _MissionCache.path, _MissionCache.mtime, _MissionCache.data = path, mt, out
    return out


def load_mission_main():
    """主线任务序列（MainIndex>0），按 MainIndex 升序返回 list。

    ★ 配置里主线是**多份重叠的链**，不能直接用 `Next` 串：
      A(10057, MI 1~72)  /  B(10425, MI 1~74) 是同一段主线的两个版本；
      C(10612) 从 MI 73 接、D(10652) 从 MI 93 接，但它们的 Next 是 0（不指向 C）。
      所以按 **MainIndex 去重、同号取 ID 最大**（= 最新版本）再升序，
      实测得到 147 个节点、MainIndex 1~147、TargetID(关卡) 单调递增。
    每项 = 解析后的任务 dict（含 main_index / target_id / awards / next ...）。
    """
    cfg = load_mission_config()
    if _MissionCache.main is not None and _MissionCache.main[0] is cfg:
        return _MissionCache.main[1]
    best = {}
    for mid, row in (cfg or {}).items():
        mi = int(row.get("main_index") or 0)
        if mi <= 0:
            continue
        cur = best.get(mi)
        if cur is None or int(row["id"]) > int(cur["id"]):
            best[mi] = row
    seq = [best[k] for k in sorted(best)]
    _MissionCache.main = (cfg, seq)
    return seq

_ITEM_FULL_CACHE = {"path": "", "mtime": -1.0, "data": None}


def load_item_config():
    """ItemConfig 完整解析（道具使用能力）。

    返回 {id: {name, func(第一个 Functions 的 ClassName), pool_ids(所有 AwardID),
    groups(道具层 AwardGroup[{weight, pool_ids}]), items(SelectAnyItem 候选),
    heros(SelectHero 候选), cond_type, cond_value, unit, sub_type}}。

    ★ 礼包能否打开由 Functions[1].ClassName 决定（无 Type/Group 字段）：
      SendAward / SelectAnyItem / SelectHero 三种可打开，其余为材料。

    ★ 结果按 mtime 缓存：这个函数要正则扫 1000 条配置，以前每次 get_item() 都重解析。
      卡类实例模型（is_card_item）在发放/迁移里会大量调用，不缓存启动要多花 20+ 秒。
    """
    import re as _re
    path = os.path.join(_CONFIG_DIR, "app.config.new.ItemConfig.lua")
    try:
        _mt = os.path.getmtime(path)
    except OSError:
        _mt = 0
    _c = _ITEM_FULL_CACHE
    if _c["data"] is not None and _c["path"] == path and _c["mtime"] == _mt:
        return _c["data"]
    src = _read_cached(path)
    out = {}
    heads = [(m.start(), m.group(1)) for m in _re.finditer(r'\[\s*(\d+)\s*\]\s*=\s*\{', src)]
    heads.append((len(src), None))
    for (st, hid), (e, _) in zip(heads, heads[1:]):
        body = src[st:e]
        def num(key, d=0):
            mm = _re.search(r'\b' + key + r'\s*=\s*(-?\d+)', body)
            return int(mm.group(1)) if mm else d
        fn = _re.search(r'ClassName\s*=\s*"([A-Za-z]+)"', body)
        # 道具层 AwardGroup：[{weight, pool_ids}]
        groups = []
        for gm in _re.finditer(r'ClassName\s*=\s*"AwardGroup"', body):
            seg = body[gm.end():]
            nxt = seg.find('ClassName = "AwardGroup"')
            seg = seg[:nxt] if nxt > 0 else seg[:1200]
            w = _re.search(r'Probability\s*=\s*(\d+)', seg)
            pools = [int(x) for x in _re.findall(r'AwardID\s*=\s*(\d+)', seg)]
            groups.append({"weight": int(w.group(1)) if w else 1, "pool_ids": pools})
        # SelectAnyItem.Items / SelectHero.Heros
        def id_list(key):
            mm = _re.search(r'\b' + key + r'\s*=\s*\{([^}]*)\}', body)
            if not mm:
                return []
            return [int(x) for x in _re.findall(r'\d+', mm.group(1))]
        out[int(hid)] = {
            "id": int(hid),
            "func": fn.group(1) if fn else "",
            "pool_ids": [int(x) for x in _re.findall(r'AwardID\s*=\s*(\d+)', body)],
            "groups": groups,
            "items": id_list("Items"),
            "heros": id_list("Heros"),
            "cond_type": num("Type"),
            "cond_value": num("Value"),
            "unit": num("Unit", 1),
            "sub_type": num("SubType"),
            # ShowType 201-204=军旗材料1-4 / 205 材料箱 / 206 自选材料箱 /
            # 207 图纸 / 208 自选图纸箱（军旗分解返还只退材料+铜币，不退图纸）
            "show_type": num("ShowType"),
            "quality": num("Quality"),
            # HeroType>0 = 该道具是某武将的卡（替身/武将卡）；礼包类 HeroType=-1
            "hero_type": num("HeroType", -1),
            "star": num("Star"),
        }
    _c["path"], _c["mtime"], _c["data"] = path, _mt, out
    return out


def get_item(item_id):
    return load_item_config().get(int(item_id))


_CARD_ITEM_CACHE = {}


def is_card_item(item_id):
    """武将卡/定缘卡：SubType=1/7 且 Unit<=1。

    客户端 `ItemData.lua` 用 `canStack = Unit > 1` 判定可堆叠；SubType=1/7
    的卡 Unit 恒为 1 → canStack=false → 客户端**不显示数量、一条 ItemData
    一个卡格**。因此服务端必须每张卡一个实例 id（N 张 = N 条 ItemData）。

    ★ 不能用 HeroType>0 排除开包型武将卡（HeroType=-1，如 29966 五星神将卡、
      20096 魏国卫军卡、24007 五星武将精选卡）。它们同样 Unit=1、同样显示在
      上阵界面「武将卡」页，按堆叠下发 count=N 时客户端只会显示 1 格且看不到
      数量（2026-10-27 用户报障：10 张 5 星神将卡只显示拥有卡 1）。

    ★ 结果按 type 缓存：`load_item_config()` 每次调用都会重新正则解析整张
      ItemConfig（无结果缓存），启动迁移要判断 200+ 个 type，不缓存会卡 30 秒。
    """
    try:
        key = int(item_id)
    except (TypeError, ValueError):
        return False
    hit = _CARD_ITEM_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        row = get_item(key) or {}
        hit = (int(row.get("sub_type") or 0) in (1, 7)
               and int(row.get("unit") or 0) <= 1)
    except (TypeError, ValueError):
        hit = False
    _CARD_ITEM_CACHE[key] = hit
    return hit



_ITEM_CATALOG_ALL = None


def _is_dummy_row(row):
    """名字为 dummy 的是未完成的占位道具（ItemConfig 里有 105 个），不是正式道具。"""
    return str((row or {}).get("name") or "").strip().lower() == "dummy"


def item_catalog_all():
    """全量道具（**含 dummy**），内部扫描结果，只算一次。"""
    global _ITEM_CATALOG_ALL
    if _ITEM_CATALOG_ALL is not None:
        return _ITEM_CATALOG_ALL
    out = []
    try:
        path = os.path.join(_CONFIG_DIR, "app.config.new.ItemConfig.lua")
        src = _read_cached(path)
        heads = [(m.start(), int(m.group(1)), m.end())
                 for m in re.finditer(r"\[\s*(\d+)\s*\]\s*=\s*\{", src)]
        heads.append((len(src), 0, len(src)))
        for idx in range(len(heads) - 1):
            st, iid, end_pos = heads[idx]
            nxt = heads[idx + 1][0]
            body = src[end_pos:nxt]
            nm = re.search(r'Name\s*=\s*"((?:\\\d{1,3}|[^"\\])*)"', body)
            ql = re.search(r"Quality\s*=\s*(\d+)", body)
            stp = re.search(r"SubType\s*=\s*(\d+)", body)
            fn = re.search(r'ClassName\s*=\s*"(\w+)"', body)
            out.append({
                "id": iid,
                "name": decode_lua_str(nm.group(1)) if nm else ("道具%d" % iid),
                "quality": int(ql.group(1)) if ql else 0,
                "subtype": int(stp.group(1)) if stp else 0,
                "func": fn.group(1) if fn else "",
            })
    except Exception:
        pass
    out.sort(key=lambda x: x["id"])
    _ITEM_CATALOG_ALL = out
    return out


def dummy_item_ids():
    """所有占位道具（Name == "dummy"）的 id 集合。"""
    return {x["id"] for x in item_catalog_all() if _is_dummy_row(x)}


def is_dummy_item(item_id):
    try:
        return int(item_id) in dummy_item_ids()
    except (TypeError, ValueError):
        return False


def item_catalog():
    """正式道具列表（**已排除 dummy**），管理端选择器 / 道具列表用。"""
    return [x for x in item_catalog_all() if not _is_dummy_row(x)]


# ---------------------------------------------------------------- 武将/装备目录（邮件发放台用）
# 口径（用户 2026-10-28 拍板）：邮件可发 4/5/6★ 有效武将、紫(4)/神(5)品质且
# 星级在可拥有区间（3~5★）的装备、洗练石（改造石 20002，普通道具通道）。

_HERO_CATALOG_ALL = None

# 有效武将口径 = 客户端图鉴 CollectManager:initDataOnLogin 的过滤条件：
#   10001 <= ID <= 19979 且 Origin >= 1 且 Group >= 0（ClientConfig 253/272/1202 行）
#   （排除 149xx Name="dummy" 占位行与 18xxx 章鱼/魔将等 Origin=-1 怪物行）
HERO_ID_MIN, HERO_ID_MAX = 10001, 19979


def is_valid_hero(hero_id):
    """该 ID 是否是客户端认可的有效武将（图鉴口径）。"""
    try:
        r = load_hero_config().get(int(hero_id))
    except (TypeError, ValueError):
        return False
    if not r:
        return False
    if not (HERO_ID_MIN <= int(r.get("ID") or 0) <= HERO_ID_MAX):
        return False
    if int(r.get("Origin") or 0) < 1 or int(r.get("Group") or 0) < 0:
        return False
    if "dummy" in str(r.get("Name") or "").lower():
        return False
    return True


def hero_catalog_all():
    """全量**有效**武将目录（HeroConfig，图鉴口径），{id, name, star, origin}。"""
    global _HERO_CATALOG_ALL
    if _HERO_CATALOG_ALL is not None:
        return _HERO_CATALOG_ALL
    out = []
    for hid, r in load_hero_config().items():
        try:
            hid = int(hid)
        except (TypeError, ValueError):
            continue
        if not is_valid_hero(hid):
            continue
        out.append({
            "id": hid,
            "name": get_hero_name(hid),
            "star": int(r.get("Quality") or 0),      # Quality 即星级（1~6）
            "origin": int(r.get("Origin") or 0),     # 原始配置值（口径混杂，仅审计用）
        })
    out.sort(key=lambda x: x["id"])
    _HERO_CATALOG_ALL = out
    return out


def hero_catalog(stars=(4, 5, 6)):
    """可发放武将：指定星级（默认 4/5/6★）的有效武将。"""
    ss = {int(s) for s in stars}
    return [x for x in hero_catalog_all() if x["star"] in ss]


_EQUIP_CATALOG_ALL = None


def equip_catalog_all():
    """全量装备目录（EquipConfig），{id, name, quality, star, ownable}。"""
    global _EQUIP_CATALOG_ALL
    if _EQUIP_CATALOG_ALL is not None:
        return _EQUIP_CATALOG_ALL
    from . import growth   # 延迟导入：growth 顶层依赖配置模块，避免环
    out = []
    for eid, r in (_equip_config() or {}).items():
        try:
            eid = int(eid)
            out.append({
                "id": eid,
                "name": get_equip_name(eid),
                "quality": int(r.get("Quality") or 0),   # 4=紫 5=神(黄)
                "star": int(r.get("Star") or 0),
                "ownable": growth.is_equip_ownable(eid),
            })
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["id"])
    _EQUIP_CATALOG_ALL = out
    return out


def equip_catalog(qualities=(4, 5)):
    """可发放装备：紫(4)/神(5)品质、且星级在可拥有区间（is_equip_ownable）。"""
    qs = {int(q) for q in qualities}
    return [x for x in equip_catalog_all() if x["quality"] in qs and x["ownable"]]


REFINE_STONE_ITEM = 20002   # 洗练石（ItemConfig 名"改造石"，EquipReformStone 消耗的道具）
