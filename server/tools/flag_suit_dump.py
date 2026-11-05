# -*- coding: utf-8 -*-
"""军旗套装配置（FlagSuitConfig）全表 dumps——排查/核对用。"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"D:/Cocos_Project/HYSG/env/_work/rt_all/app.config.FlagSuitConfig.lua"
TYPE_NAME = {1: "攻击", 2: "防御", 9: "生命", 4: "连击", 6: "反击", 7: "暴击", 8: "爆伤",
             3: "速度", 5: "格挡", 12: "增伤", 13: "免伤", 20: "韧性", 21: "闪避", 22: "命中"}
FACTION = {1: "群雄", 2: "蜀汉", 3: "东吴", 4: "曹魏"}
# 用户 2026-10-27 拍板：只有阵营四套 + 战鼓的 2/4 件百分比计入属性面板
ATTR_SUIT_GIDS = {1100, 1101, 1102, 1103, 1104}


def dec(s):
    return re.sub(r"\\([0-7]{1,3})", lambda m: chr(int(m.group(1), 8)), s)


def main():
    src = open(SRC, encoding="utf-8").read()
    heads = [(m.start(), m.group(1)) for m in re.finditer(r"\[(\d+)\]\s*=\s*\{", src)]
    heads.append((len(src), None))
    for (st, gid), (e, _) in zip(heads, heads[1:]):
        if gid is None:
            break
        body = src[st:e]
        name_m = re.search(r'Name\s*=\s*"([^"]*)"', body)
        origin_m = re.search(r"Origin\s*=\s*(\d+)", body)
        cnt_m = re.search(r"Count\s*=\s*\{([^}]*)\}", body)
        app_m = re.search(r"AddPowerPer\s*=\s*\{([^}]*)\}", body)
        add_m = re.search(r"Additions\s*=\s*\{(.*?)\n    \}", body, re.S)
        name_s = dec(name_m.group(1)) if name_m else "?"
        if name_s == "dummy":
            continue
        cnt = re.findall(r"(\d+)", cnt_m.group(1)) if cnt_m else []
        app = re.findall(r"(-?\d+)", app_m.group(1)) if app_m else []
        tiers = re.findall(r"\{([^{}]*)\}", add_m.group(1)) if add_m else []
        faction = FACTION.get(int(origin_m.group(1)), "不限") if origin_m else "不限"
        in_attr = int(gid) in ATTR_SUIT_GIDS
        print("[%s] %s（势力:%s）%s" % (gid, name_s, faction,
                                        "  ← 百分比计入属性" if in_attr else ""))
        for i, c in enumerate(cnt):
            parts = []
            if i < len(tiers):
                for t, v, m0 in re.findall(
                        r"Type\s*=\s*(\d+),\s*Value\s*=\s*(-?\d+),\s*Mode\s*=\s*(\d+)", tiers[i]):
                    parts.append("%s+%s%s" % (TYPE_NAME.get(int(t), "Type" + t), v,
                                              "%" if m0 == "1" else ""))
            if i < len(app):
                parts.append("战力%%%s%%" % app[i])
            print("   %s件: %s" % (c, "、".join(parts) if parts else "—"))


if __name__ == "__main__":
    main()
