"""抽卡配置：LotterySetConfig / AwardsConfig 的 Lua 解析 + 奖池掷取。

为什么自己解析 Lua 而不是用 04_config_tables 的 JSON 快照：
  · JSON 快照有编码损坏（HeroConfig/ItemConfig 的 Name 全是乱码）且可能丢行；
  · LotterySetConfig/AwardsConfig 里 `FirstAwards/SpecialAwards/FirstNationAwards`
    是嵌套表，game_config._parse_lua_table（逐行扁平解析）拿不到；
  · 客户端读的就是 env/_work/rt_all 下的 Lua，服务端同源最稳。

只实现配置表用到的语法子集：table / number / string（含 \\ddd 转义）/ true/false/nil。
"""
import os
import random
import re
import time

_CONFIG_DIR = os.environ.get("SGHY_CONFIG_DIR", r"D:/Cocos_Project/HYSG/env/_work/rt_all")

# 参与抽卡的配置行（101~129 武将招募，201~205 装备寻宝）。
# 105~128 是 2014 年的「贤才令」限时活动（StartDate/EndDate 已过期），不暴露。
HERO_LOTTERY_TYPES = (101, 102, 103, 104, 129)
EXPLORE_LOTTERY_TYPES = (201, 202, 203, 204, 205)
LOTTERY_TYPES = HERO_LOTTERY_TYPES + EXPLORE_LOTTERY_TYPES

# 招募令牌 / 寻宝令牌（ItemConfig）
RECRUIT_TOKEN = 20010     # 招贤令
EXPLORE_TOKEN = 20009     # 远洋令

# 20 抽保底用的 6★ 集合（元宝线首次十连也用它：用户口径「总池任意一个 6★」）
SIX_STAR_POOL = 1004

_CACHE = {}
_LAST_STAT = {}


# ---------------------------------------------------------------- Lua 解析

def _decode_lua_string(raw):
    """Lua 字符串字面量（不含引号）→ Python str。

    quick 配置把 UTF-8 字节写成十进制转义（\\229\\133\\131），按十进制逐字节还原。
    """
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c != '\\':
            out.extend(c.encode('utf-8'))
            i += 1
            continue
        i += 1
        if i >= n:
            break
        e = raw[i]
        if e.isdigit():
            j = i
            while j < n and raw[j].isdigit() and j - i < 3:
                j += 1
            out.append(int(raw[i:j]) & 0xFF)
            i = j
            continue
        mapping = {'n': b'\n', 't': b'\t', 'r': b'\r', 'a': b'\a', 'b': b'\b',
                   'f': b'\f', 'v': b'\v', '\\': b'\\', '"': b'"', "'": b"'"}
        out.extend(mapping.get(e, e.encode('utf-8')))
        i += 1
    return out.decode('utf-8', 'replace')


class _Parser:
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)
        self._num = re.compile(r'-?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+)')
        self._ident = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

    def _ws(self):
        while self.i < self.n:
            c = self.s[self.i]
            if c in ' \t\r\n\ufeff':
                self.i += 1
            elif self.s.startswith('--', self.i):
                if self.s.startswith('--[[', self.i):
                    end = self.s.find(']]', self.i + 4)
                    self.i = (end + 2) if end >= 0 else self.n
                else:
                    end = self.s.find('\n', self.i)
                    self.i = self.n if end < 0 else end + 1
            else:
                return

    def _expect(self, ch):
        self._ws()
        if not self.s.startswith(ch, self.i):
            raise ValueError("expect %r at %d" % (ch, self.i))
        self.i += len(ch)

    def value(self):
        self._ws()
        c = self.s[self.i] if self.i < self.n else ''
        if c == '{':
            return self.table()
        if c in '"\'':
            return self.string()
        m = self._num.match(self.s, self.i)
        if m:
            self.i = m.end()
            txt = m.group(0)
            if txt.lower().startswith(('0x', '-0x')):
                return int(txt, 16)
            return float(txt) if ('.' in txt or 'e' in txt or 'E' in txt) else int(txt)
        for word, val in (('true', True), ('false', False), ('nil', None)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val
        raise ValueError("unexpected %r at %d" % (c, self.i))

    def string(self):
        q = self.s[self.i]
        self.i += 1
        start = self.i
        while self.i < self.n:
            c = self.s[self.i]
            if c == '\\':
                self.i += 2
                continue
            if c == q:
                raw = self.s[start:self.i]
                self.i += 1
                return _decode_lua_string(raw)
            self.i += 1
        raise ValueError("unterminated string")

    def key(self):
        self._ws()
        c = self.s[self.i] if self.i < self.n else ''
        if c == '[':
            self.i += 1
            k = self.value()
            self._expect(']')
            self._expect('=')
            return k
        m = self._ident.match(self.s, self.i)
        if m:
            save = self.i
            self.i = m.end()
            self._ws()
            if self.i < self.n and self.s[self.i] == '=':
                self.i += 1
                return m.group(0)
            self.i = save
            return None
        if c in '"\'':
            save = self.i
            k = self.string()
            self._ws()
            if self.i < self.n and self.s[self.i] == '=':
                self.i += 1
                return k
            self.i = save
            return None
        return None

    def table(self):
        self._expect('{')
        arr, mp = [], {}
        while True:
            self._ws()
            if self.i >= self.n:
                raise ValueError("unterminated table")
            if self.s[self.i] == '}':
                self.i += 1
                break
            k = self.key()
            v = self.value()
            if k is None:
                arr.append(v)
            else:
                mp[k] = v
            self._ws()
            if self.i < self.n and self.s[self.i] in ',;':
                self.i += 1
                continue
            self._ws()
            if self.i < self.n and self.s[self.i] == '}':
                self.i += 1
                break
            raise ValueError("expect , or } at %d" % self.i)
        if arr and mp:
            for idx, v in enumerate(arr, 1):
                mp[idx] = v
            return mp
        return arr if arr else mp


def _parse_file(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()
    i = text.find('{')
    if i < 0:
        return {}
    p = _Parser(text)
    p.i = i
    return p.table()


def _load(name):
    """按 mtime 缓存解析（配置热改无需重启，同 game_config 的策略）。"""
    path = os.path.join(_CONFIG_DIR, 'app.config.%s.lua' % name)
    now = time.monotonic()
    hit = _CACHE.get(path)
    if hit and now - _LAST_STAT.get(path, 0.0) < 1.0:
        return hit[1]
    _LAST_STAT[path] = now
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0
    if hit and hit[0] == mt:
        return hit[1]
    data = _parse_file(path)
    _CACHE[path] = (mt, data)
    return data


def lottery_set():
    """{type: row}，row 含 Count/Currency/Price/Group/FreeHour/.../Awards"""
    return _load('LotterySetConfig')


def awards():
    """{pool_id: row}，row 含 Awards=[AwardGroup{Probability, Awards=[ref...]}]"""
    return _load('AwardsConfig')


def clear_cache():
    _CACHE.clear()
    _LAST_STAT.clear()


# ---------------------------------------------------------------- 奖池

def as_refs(field):
    """把 FirstAwards/SpecialAwards/... 规范成 ref 列表。

    配置里可能是：[]（空）、""（空）、[{ClassName=...}]（列表）、
    {ClassName=...}（单条）、[1061,1062]（池 id 列表）。
    """
    if isinstance(field, list):
        return field
    if isinstance(field, dict):
        if field.get('ClassName'):
            return [field]
        return list(field.values())
    if isinstance(field, int):
        return [field]
    return []


def groups(pool_id):
    """[(weight, [ref...])]；Awards 里可能是 AwardGroup 也可能是裸 ref。"""
    row = awards().get(int(pool_id))
    if not row:
        return []
    out = []
    for g in (row.get('Awards') or []):
        if isinstance(g, dict) and g.get('ClassName') == 'AwardGroup':
            out.append((int(g.get('Probability') or 0), g.get('Awards') or []))
        elif isinstance(g, dict):
            out.append((int(g.get('Probability') or 1), [g]))
        elif isinstance(g, int):
            out.append((1, [{'ClassName': 'QuoteAward', 'AwardID': g}]))
    return out


def _kind_of(type_id):
    """把 ObjectAward.Type 归类成 hero / equip / item。"""
    from . import growth
    try:
        if growth.find_hero_row(type_id):
            return 'hero'
    except Exception:
        pass
    try:
        if growth.equip_row(type_id) and growth.is_equip_ownable(type_id):
            return 'equip'
    except Exception:
        pass
    return 'item'


def _roll_ref(ref, rng):
    """单个 ref -> [award dict]。QuoteAward 递归；AwardGroup 展开其全部子项。"""
    if not isinstance(ref, dict):
        return []
    cn = ref.get('ClassName')
    if cn == 'QuoteAward':
        return roll_pool(ref.get('AwardID'), rng)
    if cn == 'AwardGroup':
        out = []
        for r in (ref.get('Awards') or []):
            out.extend(_roll_ref(r, rng))
        return out
    if cn == 'ObjectAward':
        t = int(ref.get('Type') or 0)
        c = int(ref.get('Count') or 1)
        return [{'kind': _kind_of(t), 'type': t, 'count': max(1, c)}]
    return []


def roll_pool(pool_id, rng=None):
    """按权重选一个 AwardGroup，返回该组的全部奖励（通常 1 条）。"""
    rng = rng or random
    gs = groups(pool_id)
    if not gs:
        return []
    total = sum(w for w, _ in gs)
    if total <= 0:
        return []
    pick = rng.random() * total
    acc = 0
    chosen = gs[-1][1]
    for w, refs in gs:
        acc += w
        if pick < acc:
            chosen = refs
            break
    out = []
    for ref in chosen:
        out.extend(_roll_ref(ref, rng))
    return out


def roll_refs(refs, rng=None):
    """从 ref 列表里选一条掷取（配置里绝大多数只有一个 ref）。"""
    rng = rng or random
    refs = [r for r in (refs or []) if r]
    if not refs:
        return []
    ref = refs[0] if len(refs) == 1 else rng.choice(refs)
    return _roll_ref(ref, rng)


def pool_hero_types(pool_id):
    """池内所有武将 type（递归展开 QuoteAward），用于首次十连的 6★ 集合。"""
    seen = set()

    def walk(pid):
        if pid in seen:
            return set()
        seen.add(pid)
        out = set()
        for _, refs in groups(pid):
            for ref in refs:
                cn = ref.get('ClassName') if isinstance(ref, dict) else None
                if cn == 'QuoteAward':
                    out |= walk(ref.get('AwardID'))
                elif cn == 'ObjectAward':
                    t = int(ref.get('Type') or 0)
                    if _kind_of(t) == 'hero':
                        out.add(t)
        return out

    return walk(int(pool_id))
