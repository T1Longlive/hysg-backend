# sghy-server —— 三国的后裔（youguTV）复用项目专用游戏后端

> Python 3.13 + asyncio · 单进程起步 · 客户端（quick-cocos2d-x 2.2.6 + Lua）**零改动**
> 替代早期诊断用的 mock 服务端（已随 2026-11-02 环境整理移除，实现见 git 历史），协议与业务口径同源。

## 这是什么

原 APK 的服务端代码不存在于包内，本项目从协议逆向还原了一个与真服行为一致的后端。
本仓库是它的**正式工程化形态**：asyncio 事件循环替代线程-per-连接、真 protobuf 库替代
手写编解码（进行中）、可配置存档路径替代硬编码，业务规则与配置口径全部继承自已验证的 mock。

## 快速开始

**启动（两个脚本）**：先双击 `D:/Cocos_Project/HYSG/env/start_server.bat` 起服务端，
再双击 `env/start_client.bat` 起客户端（会先关掉已运行的 player.exe）。

**手动两条命令**：

```bash
# 1) 服务端（依赖装在 workbuddy 托管 Python：protobuf / aiohttp / aiosqlite / SQLAlchemy）
cd D:/Cocos_Project/HYSG/server
C:/Users/67610/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m sghy_server

# 2) 客户端（另开一个窗口）
cd D:/Cocos_Project/HYSG/env/quick_src/quick-cocos2d-x-2.2.6-release/player/proj.win32/Debug
./player.exe -workdir 'D:\Cocos_Project\HYSG\env\sghy_run' -file 'scripts/main.lua' -write-debug-log
```

- TCP 游戏服务：`0.0.0.0:2724`（客户端 serverlist 的 game_port 指向这里）
- HTTP 网关：`0.0.0.0:8000`（`/api/getserverlist`、`/api/getupdateconfig`、`/api/getannounce`、`/api/gm`）
- 日志：服务端 `logs/server.log`（追加）+ stdout；客户端 `env/sghy_run/debug.log`
- 客户端无需任何改动（mock_redirect 已指向 127.0.0.1:8000）

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SGHY_TCP_PORT` / `SGHY_HTTP_PORT` | 2724 / 8000 | 端口 |
| `SGHY_PLAYER_DATA` | `D:/Cocos_Project/HYSG/env/player_data.json` | JSON 存档（仅首次空库迁移用） |
| `SGHY_DB_PATH` | `D:/Cocos_Project/HYSG/server/data/sghy.db` | SQLite 库（运行时唯一真相） |
| `SGHY_FLUSH_DELAY` | 1.5 | 脏数据延时批量落库间隔（秒） |
| `SGHY_CONFIG_DIR` | `D:/Cocos_Project/HYSG/env/_work/rt_all` | Lua 配置表目录 |

### 回归测试

```bash
python tools/golden_test.py           # 请求读取：typed vs 字节级双路等价（40 断言）
python tools/builder_golden_test.py   # 回包构造：legacy vs 原生 protobuf 语义等价（94 断言）
python tools/battle_validate_test.py  # 战斗校验：合法放行 + 六类伪造拒绝（14 断言）
python tools/lottery_unit_test.py     # 招募/寻宝：首抽/首十/20 抽保底/令牌/折扣/拒绝（10 断言）
python tools/collect_unit_test.py     # 图鉴/收集：补种/进度/领取/重复领取/新点亮（7 断言）
python tools/smoke_client.py          # 全链路：登录→选角→EnterGame→用符→战斗结算
python tools/lottery_tcp_test.py 127.0.0.1 12725  # 招募/寻宝端到端（另起测试服，见文件头）
python tools/collect_tcp_test.py 127.0.0.1 12726  # 图鉴/收集端到端（另起测试服，见文件头）
```

## 目录结构

```
sghy_server/
├── net/
│   ├── framing.py        # 协议帧编解码（请求/响应两种线型）
│   ├── tcp_server.py     # asyncio TCP 服务 + 会话注册表
│   └── http_gateway.py   # aiohttp HTTP 网关（serverlist/公告/GM）
├── proto/
│   └── ps_protobuf_pb2.py# protoc 生成（proto/ps_protobuf.proto，358 message）
├── game/
│   ├── core.py           # 业务核心：opcode dispatch + GM + 存档写点（build_* 委托 builders_pb）
│   ├── builders_pb.py    # 回包原生 protobuf 构造（22 个 builder）
│   ├── legacy_builders.py# mock 字节级 builder 只读快照（黄金测试对照）
│   ├── battle_validate.py# 战斗结果合理性校验（不重跑模拟）
│   ├── proto_map.py      # opcode→消息映射 + typed 请求助手（黄金测试护航）
│   ├── growth.py         # 成长/属性/战力公式（mock 移植）
│   ├── game_config.py    # 配置表解析（mock 移植）
│   ├── lottery_config.py # 招募/寻宝奖池：LotterySetConfig + AwardsConfig（Lua 直读 + 权重掷取）
│   ├── collect_config.py # 图鉴/收集：ShowInCollect 集合 + CollectionAwardConfig 里程碑
│   ├── challenge.py      # 挑战副本：圣兽岛/金银岛/阵营副本（5327/5034/5037/6007/6008/6009，2026-10-29）
│   └── session.py        # 连接会话（登录首包、帧泵、dispatch 适配）
├── data/
│   ├── models.py         # ORM 模型（players/heroes/equips/item_stacks/fate/level_records/accounts）
│   ├── db.py             # 异步 SQLite 引擎
│   ├── repo.py           # 仓库：dict ↔ 行双向映射 + diff 增量落库 + JSON 迁移/导出
│   └── store.py          # 存档门面：脏标记 + 延时批量刷库
└── settings.py           # 端口/路径配置
```

## 玩家存档（真实持久化）

- **SQLite 库**：`data/sghy.db`（`SGHY_DB_PATH` 可覆盖），权威字段落真实列、其余进
  `extra_json`，与 `PLAYER_DATA` dict 形状无损 round-trip。
- **首次启动自动迁移**：空库 → 从 `env/player_data.json` 一次性导入（日志有
  `空库 -> 已从 JSON 迁移`）；此后**库是唯一真相**，JSON 不再被写。
- **写策略**：业务 29 处写点全部走 `mark_dirty()` → 1.5 秒（`SGHY_FLUSH_DELAY`）合并成
  **单事务 diff upsert**（只写变化的行、删除消失的行）；退出前最终 flush。
- **并发安全**：业务函数全同步（dispatch 内无 await），dict 变更对事件循环原子；
  flush 先深拷贝快照再写库。落库失败不崩服务，保持 dirty 下批重试。
- **备份/互通**：`python tools/export_json.py` 随时把库存回 JSON。
- **已验证**：用符后直接查库道具已扣；杀服重启后数据从库读回（JSON 里是旧值，
  服务端内存用的是库值）；重启全程客户端 0 报错。

## 协议要点（客户端→服务端）

| 项 | 格式 |
|---|---|
| 登录首包 | 裸 `Base64(JSON + "\n")`，无长度头 |
| 请求帧 | `[4B 10008][4B len][4B opCode][4B protoIndex][pb]`，`len = 8 + len(pb)` |
| 响应帧 | `[4B len][4B opCode][pb]`，`len = 4 + len(pb)`（**无 magic、无 protoIndex**） |
| 字节序 | 全部小端 |

## 架构决策（为什么这么做）

1. **Python 不换语言**：2671 行已验证业务逻辑零风险迁移；1000 CCU（5–20k msg/s）asyncio 单进程余量一个数量级。
2. **读路径已全量真 protobuf**：`proto_map.py` 把 24 个 opcode 映射到 protoc 生成类，
   dispatch 的读取点全部走 typed 助手（`req_id`/`req_function`/…），字节级仅作兜底。
   历史 bug（`FunctionData.param` 是 string 字段，被 varint 误读成 53）从此不可能复发。
   等价性由 `tools/golden_test.py` 双路比对保证（38 断言全过）。
3. **写路径也已原生 protobuf**：22 个 builder 全部用 protoc 生成类构造（`builders_pb.py`），
   `legacy_builders.py` 保留字节级快照作黄金测试对照（121 断言语义等价）。
   迁移中修正了 mock 时代的**三处字段错位**（详见下方「字段错位修正」）。
4. **存储已真实化**：SQLAlchemy 2.0 + SQLite（`data/` 包），diff 增量 + 延时批量；
   PostgreSQL 切换只改连接串（模型层无需动）。

## 已验证能力（2026-10-21）

- [x] 登录首包 → LoginOK(100) → SelectCharacter(10001) → EnterGame(102) → 客户端进主界面，0 LUA ERROR
- [x] EnterGame 完整下发：70 武将 / 125 装备 / 19 道具 / 阵容 / 关卡记录
- [x] 用经验符（5008）：道具扣减(451) + 武将数据(453) + 角色同步(200) + ack(5008)
- [x] HTTP：serverlist（含 `data.platform`，缺了客户端直接崩）/ 公告 / GM
- [x] 冒烟客户端回归（`tools/smoke_client.py`）
- [x] **存档真实持久化**：SQLite 落库、JSON 一次性迁移、杀服重启数据从库读回、0 刷库失败
- [x] **读路径全量 typed protobuf**：24 个 opcode 映射生成类，黄金测试 38 断言全过，
  真实客户端登录/用符 0 报错
- [x] **写路径全量原生 protobuf**：22 个 builder 用生成类构造，builder 黄金测试 121 断言
  （70 武将 + 30 装备 + 全部叶子/战斗/登录消息）语义等价，真实客户端 0 报错
- [x] **战斗结果合理性校验**（5031）：关卡存在性 / 结果与星级值域 / 用时区间 / 伤害区间 /
  阵容归属，14 单元断言 + 冒烟端到端（合法胜利发奖、伪造星级被拒且零奖励）
- [x] **招募 / 寻宝（5040/5041）**：10 条记录下发、首抽、首次十连 6★/紫装、20 抽保底、
  免费冷却、招贤令/远洋令、十连折扣；`lottery_unit_test` 10 断言 + `lottery_tcp_test` 6 断言

## 战斗校验（battle_validate.py）

PVE 结算由客户端计算是原版设计（真服亦然），服务端不重跑模拟，只做**防伪合理性判定**：

| 规则 | 判定 | 阈值（宽松防伪界） |
|---|---|---|
| level_exists | 关卡在 LevelSetConfig 有敌人配置 | — |
| result_domain | result ∈ {0,1} | — |
| star_domain | star ∈ {0,1}（实测客户端胜负只报 1/0）；失败不得报星 | — |
| time_range | 胜利用时 ∈ [1, 3600] 秒 | `TIME_MIN_S` / `TIME_MAX_S` |
| damage_range | 胜利总伤 ∈ [敌方总血×0.3, 敌方总血×20+10万] | `DMG_FLOOR_RATIO` / `DMG_CEIL_RATIO` |
| lineup | 上报 myGroup 英雄必须是本人的（≤5 人）；战力偏差 ≤50% | `POWER_TOLERANCE` |

**拒绝语义**：`ShowMessage(5002)` 弹真实原因 + 回零奖励 LevelResult（不发进度、不加经验），
即「按失败处理」。阈值全部集中在文件顶部，调平衡只改那里。

## 招募 / 寻宝（5040 / 5041，2026-10-22）

主城两个入口共用同一套协议与配置：**武将招募**（`EmployeePanel`）与**装备寻宝**（`BExplorePanel`）。

| 项 | 实现 |
|---|---|
| 记录下发 | `5041` → `LotteryRecordDatas`（10 条：101/102/103/104/129 + 201~205），同时挂在 `EnterGame.lotteryRecords`（字段 8） |
| 请求 | `UseLotteryData{id=记录 id, useItem}` → 招募回 `476 AllHeroMessage`（key=404），寻宝回 `5040 AwardResult` + 逐件 `403 NewEquip` |
| 存档 | `PLAYER_DATA["player"]["lottery"]`（进 `player.extra_json`，不需新表） |
| 免费 | 只做冷却：101 每 24h、102 每 3h、201 每 2h、202 每 24h；抽完记录 `next_free`，客户端只认剩余秒数 |
| 首抽 | 101 → 池 2003（4★ 16 选 1）；102 → 固定张星彩 2★；201 → 固定 31111 装备；**免费抽也算消耗首抽** |
| 首次十连 | 104 → 池 1004 的 6★ 集合随机 1 个（替换最后一抽）；203 → 池 3320 紫装；129/103/204/205 的 `FirstAwards` 与普通池相同，不加料 |
| 20 抽保底 | 按 `Group` 共用计数（元宝线 Group=1 含 101/104/129）；累计第 20 抽从 `SpecialAwards`（1030，42 个 5★）出；与首次十连撞同一抽时 6★ 优先、保底挪前一抽 |
| 令牌 | 招贤令 20010（101 单抽 1 个 / 129 十连 10 个）、远洋令 20009（202 单抽 1 个 / 205 十连 10 个）；用完发 `499 DeleteProp(typeID=2)` |
| 折扣 | 只作用于 104 元宝十连：`GM set_lottery_discount 50 3`（5 折 3 次）；登录/变更推 `5318 LotteryActivityInfo` + `5041` |

**拒绝语义**：资源不足/记录失效 → `5002` 说明 + 不发 476。客户端本身没有失败回包通道，
所以配了 override 看门狗 `env/sghy_run/override/recruit_guard.lua`：结果面板 12 秒没收到回包
自动解锁输入 + 关闭面板，任何异常都不会再锁死游戏。

**测试**：`tools/lottery_unit_test.py`（10 断言，fake session）+ `tools/lottery_tcp_test.py`
（真服务端 6 断言）。回归时 `builder_golden_test` 的 EnterGame 白名单已加字段 8/11/45。

## 图鉴 / 收集（560 / 565 / 10115，2026-10-22）

客户端 `CollectManager` 的图鉴是「**曾经获得过**」：武将当升星材料吃掉、装备分解后，
对应图鉴仍要亮。所以服务端单独存 type 列表，不能只从当前背包推导。

| 项 | 实现 |
|---|---|
| 数据 | `PLAYER_DATA["player"]["collect"] = {heroes:[type], equips:[type], awards:{"1_1":[15,...]}}`（`player.extra_json`，无新表） |
| 点亮范围 | `HeroConfig.ShowInCollect=1`（255 条，按 `Origin` 1~4 分 4 组）；`EquipConfig.ShowInCollect=1`（399 条，按 `Part` 1~4 分 4 组） |
| 进度 | 该组内已点亮的不同 type 数（与客户端 `getCollectionProgress` 同口径）；奖励里程碑见 `CollectionAwardConfig`（武将 15/25/35/45/55，装备 20/40/60/80/100，头盔只有 20/40/60） |
| 登录下发 | `EnterGame.heroRecords(11)` / `equipRecords(45)` / `collectionAwardRecords(46)`；登录时把当前武将/装备并进记录（兼容老存档） |
| 实时点亮 | 新武将 → `560 HeroRecordData`；新装备 → `565 EquipRecordData`。挂点：招募、GM 发放、碎片合成、**升星换 type** |
| 领取 | `10115 ReceiveCollectionAwardRequestData{type=1武将/2装备, subType=Origin/Part, milestoneCount}` → 校验进度/未领取 → 发奖 + `10115 CollectionAwardRecordData`（回已领取列表）+ `301 AwardResult` 弹窗 + `451/200` |
| 失败 | 进度不足/重复领取 → **仍回 10115**（客户端靠它清 `_requestingCollectionAward`）+ `5002` 原因，不发奖 |
| 奖励 | `PropertyAward`（Vigour/Coin）→ 玩家属性；`ObjectAward`（招贤令/远洋令/装备碎片）→ 道具；头像框 55025~55028 当前已全量解锁，`301` 用 type=0 属性槽显示名称（HonorConfig 没有 Quality，直接发 ObjectAward 会让 AwardItem 取 nil） |
| 启动自检 | `__main__` 调 `_sanitize_player_data()`：清幽灵武将/不可拥有装备/**非 ItemConfig 的"道具"**（签到/收集奖励曾把装备 33903 塞进 items，客户端登录 LUA ERROR），并把当前库存补种进图鉴后落库 |

**测试**：`tools/collect_unit_test.py`（7 断言）+ `tools/collect_tcp_test.py`（5 断言，独立测试服）。

## 回包类型对照表（改 dispatch 回包前必查，客户端解码类型为准）

| opcode | 请求 | **响应（客户端解码）** | 备注 |
|---|---|---|---|
| 5012 穿戴装备 | PropActionMessage | **PropActionMessage**{id=装备, targetID=武将} | 回错类型会把装备配置 id 当武将 id → showFatesTip `ipairs(nil)` 崩（2026-10-21 修复） |
| 5013 卸下装备 | IDMessage | **IDMessage**{id=装备} | 回错类型碰巧能用（字段 1 同为 id）但已改正 |
| 5014/5016 强化 | IDMessage/IDInt32Message | EquipData | |
| 6021/6023/6025-27 装备养成 | IDListMessage | EquipData | |
| 5023 突破/5024 升星 | PropActionMessage | HeroData | |
| 5008 用道具 | FunctionData | FunctionData（成功）/ 无回包+5002（失败） | |

回归：`tools/equip_ack_test.py`（穿戴/卸下回包类型+语义，真实服务端往返）。

## 字段错位修正（mock 时代 bug，迁移 builder 时修复）

mock 的字节级 builder 把三个字段写进了错误槽位（客户端按 tag 解析，全被解析成垃圾/跳过）：
迁移后的 `builders_pb.py` 写到**客户端真实读取路径**，黄金测试以白名单方式验证了这三处差异：

| 数据 | mock 写入（错） | 正确槽位 | 客户端读取路径 |
|---|---|---|---|
| 缘分进度 FateFastenInfo | CharacterData.28（该槽是 `arenaShop`，解析成垃圾商店数据） | **EnterGame.28** | `UserManager.lua:587` → fateManager |
| 头像/头像框 honors | CharacterData.39（未知字段，直接跳过） | **EnterGame.39** | `UserManager.lua:599` → honorManager |
| 引导进度 guideIndex=9999 | CharacterData.21（该槽是 `bugSignTimes`） | **EnterGame.21** | UserManager → guideManager |

效果：fateManager/honorManager 从此收到真实数据（此前恒为 nil）；UpdateCharacter 推送
从 2204 字节降到 75 字节（垃圾字段消失）。

## TODO（按优先级）

1. **登录鉴权**：`accounts` 表已就绪，接登录流程 + 令牌（现在任何密码可进；用户暂缓）。
2. **Docker 化**：compose（server + postgres + redis）。
3. （可选）体力/每日次数：LevelConfig.Ep 是关卡体力消耗，真服按次扣 —— 现未扣，反刷靠上述校验兜着。

## 与早期 mock 的关系

早期诊断用的 `env/mock_server.py`（连同 `env/growth.py`、`env/game_config.py`）已在
2026-11-02 的环境整理中移除；其业务口径、公式与配置解析逻辑已完整移植到
`sghy_server/game/**`，需要对照时可从 git 历史找回。
