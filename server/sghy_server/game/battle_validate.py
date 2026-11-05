"""battle_validate —— 战斗结果合理性校验（5031 LevelEnd 上报）。

原则：**不重跑战斗模拟**（那是另一个项目的工作量），只做「这段上报有没有
明显不合理」的区间/归属判定。PVE 结算由客户端计算是原版设计（真服也是
客户端算完上报），服务端的职责是把明显伪造的上报挡在门外：

  1. level_exists —— 关卡必须在 LevelSetConfig 里有敌人配置（防伪造关卡）
  2. result_domain —— result ∈ {0,1}（客户端合法输出）
  3. star_domain  —— star ∈ {0,1}（实测客户端胜负只报 1/0，星级由服务端定）
  4. time_range   —— 胜利 useTime ∈ [1, 3600] 秒（战斗不可能 0 秒结束）
  5. damage_range —— 胜利 totalDamage ∈ [敌方总血×0.3, 敌方总血×20+10万+服务端战力×20]
                     （下限：打死人必须造成伤害；上限：挡住「报 9 亿伤害」。
                       ★ 客户端 totalHurtVal 含 overkill 全额伤害——HP 只夹到 0，
                       伤害值不截断（WarriorData:addHP），高练度一刀秒小怪时
                       总伤远超敌方总血，上限必须按我方战力留溢出裕量。
                       战力项只认服务端算的 server_power，客户端上报值不参与——
                       否则被校验方虚报战力就能自抬合格线）
  6. lineup       —— 上报的 myGroup 英雄必须是玩家自己的（≤5 人）；
                     若带 power，与服务端战力偏差 ≤50%

所有阈值都是**防伪宽松界**，不是平衡性设计：宁可放过可疑的正常玩家，
也要拦住一眼假的伪造。阈值集中在本文件顶部，调平衡只改这里。
"""

# ---- 防伪阈值（宽松界，只拦明显伪造）----
TIME_MIN_S = 1            # 胜利最短用时（秒）
TIME_MAX_S = 3600         # 最长用时（秒）
DMG_FLOOR_RATIO = 0.3     # 胜利总伤 ≥ 敌方总血 × 0.3
DMG_CEIL_RATIO = 20.0     # 胜利总伤 ≤ 敌方总血 × 20
DMG_CEIL_ABS = 100000     #   再加固定裕量（低级关卡不至于误杀）
DMG_CEIL_POWER = 20.0     #   再加战力×20（overkill 溢出裕量：一刀秒全额计伤）
POWER_TOLERANCE = 0.5     # 上报战力与服务端战力偏差 ≤ 50%
MAX_TEAM_SIZE = 5         # 上阵人数上限


def _enemy_total_hp(game_config, lid) -> int:
    """LevelSetConfig 该关全部敌人的 RealHp 之和（Scale 缩放后）。"""
    total = 0
    for ec in game_config.get_level_enemies(lid) or []:
        try:
            hp = float(ec.get("hp") or 0)
            scale = float(ec.get("scale", 100) or 100) / 100.0
            total += int(round(hp * scale))
        except (TypeError, ValueError):
            continue
    return total


def validate_level_end(game_config, report) -> tuple:
    """校验一条 LevelEnd 上报。report 字段：

      lid               关卡 id
      result            1=胜 0=败
      star              上报星级
      total_damage      int
      use_time          秒（浮点可）
      my_die_count      我方阵亡数（battleRecord.myDieCout，可 None）
      my_group_ids      上报我方英雄实例 id 列表（可 []）
      my_group_power    上报我方战力（可 None）
      server_power      服务端当前同阵容战力（可 None，用于对比）

    返回 (ok, rule, reason)：
      ok=True  通过；rule/reason 供日志
      ok=False 拒绝；rule=违规规则名，reason=给玩家看的提示
    """
    lid = report.get("lid")
    result = report.get("result")
    star = report.get("star")

    # 1) 关卡存在
    enemies = game_config.get_level_enemies(lid) or []
    if not enemies:
        return False, "level_exists", "关卡不存在（%s）" % lid

    # 2) result 值域
    if result not in (0, 1):
        return False, "result_domain", "战斗结果不合法"

    # 3) star 值域（客户端合法输出只有 0/1）
    if star not in (0, 1):
        return False, "star_domain", "星级不合法"

    if result == 0:
        # 失败不领奖，星级必须为 0
        if star != 0:
            return False, "star_domain", "失败不得报星"
        return True, "loss", "失败（不发奖）"

    # ---- 以下是胜利校验 ----
    # 4) 用时区间
    try:
        use_time = float(report.get("use_time") or 0)
    except (TypeError, ValueError):
        use_time = -1
    if use_time < TIME_MIN_S or use_time > TIME_MAX_S:
        return False, "time_range", "战斗用时不合法"

    # 5) 伤害区间
    try:
        dmg = int(report.get("total_damage") or 0)
    except (TypeError, ValueError):
        dmg = -1
    enemy_hp = _enemy_total_hp(game_config, lid)
    # 溢出伤害裕量按我方战力取：战力越高打小怪 overkill 越多。
    # ★ 只认 server_power：客户端上报值参与上限 = 让被校验方自定合格线
    # （虚报战力会顺带抬高伤害上限；lineup 的 50% 偏差检查在后面兜底，
    # 但 server_power=0 时那道检查会被跳过，指望不住）。
    # server_power=0（上报 id 全非本人）→ 不给战力项，靠随后的 lineup 归属拒。
    power_ref = 0.0
    try:
        power_ref = float(report.get("server_power") or 0)
    except (TypeError, ValueError):
        power_ref = 0.0
    floor_dmg = int(enemy_hp * DMG_FLOOR_RATIO)
    ceil_dmg = int(enemy_hp * DMG_CEIL_RATIO) + DMG_CEIL_ABS + int(power_ref * DMG_CEIL_POWER)
    if dmg < floor_dmg:
        return False, "damage_range", "伤害过低，与胜利不符"
    if dmg > ceil_dmg:
        return False, "damage_range", "伤害超出合理区间（%d > %d）" % (dmg, ceil_dmg)

    # 6) 阵容归属
    ids = report.get("my_group_ids") or []
    if len(ids) > MAX_TEAM_SIZE:
        return False, "lineup", "上阵人数超限"
    owner_ids = report.get("owner_hero_ids") or set()
    for hid in ids:
        if hid not in owner_ids:
            return False, "lineup", "阵容含非本人武将（%s）" % hid
    power = report.get("my_group_power")
    sp = report.get("server_power")
    if power and sp:
        try:
            if abs(float(power) - float(sp)) / float(sp) > POWER_TOLERANCE:
                return False, "lineup", "上报战力与实际不符"
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return True, "win", "校验通过（伤害%d/敌血%d 用时%.1fs 阵亡%s）" % (
        dmg, enemy_hp, use_time, report.get("my_die_count"))
