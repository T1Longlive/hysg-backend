# 配置表编辑管线（Excel → Lua/JSON → 打包/热重载）

本文说明如何用 Excel 改游戏配置并生效。工具入口：`tools/config_tool.py`；
核心实现：`tools/config_pipeline/`。

## 1. 产物在哪

| 路径 | 作用 | 是否入库 |
|---|---|---|
| `config_src/<分组>.xlsx` | 编辑用工作簿（人改这里） | 否（.gitignore） |
| `config_data/<分组>/*.csv` + `*.schema.json` | 规范化数据源 + 列类型/已知悬空引用 | 是 |
| `build/config_gen/lua/*.lua` | 生成的 Lua（打包用） | 否（build/ 忽略） |
| `build/config_gen/manifest.json` | zip 条目 → 生成文件（相对仓库根路径） | 否 |
| `build/config_gen/history/<时间>_before/` | 每次 build 前的快照 | 否 |
| `build/youguTV_hysg.apk` | 签名后的成品 APK（未签名中间物签完自动删除；`--skip-sign` 时保留） | 否 |
| `env/_work/rt_all/app.config[.new].<表>.lua` | 服务端直读目录（build 同步） | 是 |
| `server/sghy_server/config/activities.json` | 服务端活动配置（build 直接写） | 是 |

## 2. 环境

- 配置工具用**系统 Python**（需 `openpyxl`）：`python tools/config_tool.py ...`
- 服务端单测用 `.workbuddy` 的 Python（protobuf 版本匹配），并设置 `PYTHONIOENCODING=utf-8`，
  否则中文日志在 GBK 控制台会 `UnicodeEncodeError`。
- 打包/真机：`python tools/android_repack.py`；MuMu 用
  `D:\Software\MuMuPlayer\nx_main\adb.exe`，先 `adb connect 127.0.0.1:16384`。

## 3. 支持的表

| 分组 | 表 | 去向 |
|---|---|---|
| hero | HeroConfig | 客户端 + 服务端 |
| equip | EquipConfig | 客户端 + 服务端 |
| skill | SkillConfig | 客户端 + 服务端 |
| level | LevelConfig / LevelSetConfig | 客户端 + 服务端 |
| flag | FlagPartConfig / FlagSuitConfig / FlagDrawConfig / FlagDrawLevelConfig / FlagPartSpecialSyntheticConfig | 客户端 + 服务端 |
| sign | SignAwardConfig | 客户端 + 服务端 |
| activity | ActivityShowConfig | 客户端（改完需重打包） |
| activity | activities.json（9 个活动） | 仅服务端（热重载 + 重新登录） |

新增表：在 `tools/config_pipeline/registry.py` 加一个 `TableSpec`（约束/外键/备注），
然后 `export --tables <新分组>` 生成 CSV/Schema/工作簿。

## 4. 标准流程

```bash
# 0) 首次或基线更新后：导出工作簿
python tools/config_tool.py export                 # 全部；或 --tables hero,equip

# 1) 编辑 config_src/*.xlsx

# 2) 校验（结构/类型/主键/外键/范围/行增删）
python tools/config_tool.py validate
python tools/config_tool.py diff                   # 相对 CSV 基线改了哪些

# 3a) 只改服务端可热读的表：构建 + 热重载（不需要重打包）
python tools/config_tool.py build --tables skill

# 3b) 需要客户端生效 / 一键全流程（构建 + 重载 + 重打包 + 安装 + 启动）
python tools/config_tool.py ship
#    等价于 build --repack --install --launch --server-host 192.168.1.5:8000

# 4) 回滚到上一次 build 前
python tools/config_tool.py restore --last
```

Windows 下可直接双击/调用 `tools\config_build.bat`（内部执行 `ship`）。

`build`/`validate` 前会做预检：缺 schema 或工作簿时给出可直接执行的 export 命令。

## 5. Excel 约定（Lua 表）

- 首列 `#key` 为主键，不可改；新增行填未占用的整数。
- 列名/列顺序由 `_schema` 决定，不可增删改名。
- 嵌套字段以 JSON 写单元格：`{}`、`[1,2]`、`{"Type":1,"Value":62}`。
- 删除行：该行 `_action` 填 `delete`。
- 空单元格在非字符串列视为错误（防止误删）。

## 6. 活动配置（activities.json）

工作簿 `config_src/activity.xlsx` 中：

- `<活动名>`（如 `online_award`）：字段表 `字段 | 值 | 类型`，`window` 等嵌套对象写 JSON。
- `<活动名>.<数组名>`（如 `online_award.grades`）：一行=数组一个元素，**行顺序即数组顺序**，
  删除行填 `_action=delete`。
- 校验：`enabled`、时间窗、`round_days`、奖励 `kind`（object 的 type 必须在 ItemConfig；
  property 的 prop 仅限 Dollar/Coin/Vigour/EP/HeroCoin）、转盘权重 >0、折扣 1..10、
  商店轮次 `HH:MM`、档位阈值排序（仅警告）。
- `activities.json` 只被服务端读取：`build` 后热重载即可，**客户端需重新登录**才会拉到新活动数据。
- 基线里手写的紧凑数组在首次写入时会规范化（数据不变）；无改动时不会重写文件。

## 7. 约束与坑

1. **LevelSetConfig 行序不能动**：服务端解析器会把“连续 1..n 的顶层键”折叠成 list；
   `FlagDrawLevelConfig`、`ActivityShowConfig` 本身就属于这种形态。工具自检会逐值比对
   （显示 `ok(folded)`），生成物与源数据行序不一致会直接报错。
2. **服务端缓存**：成长表/战力在进程内缓存，`build` 会自动调用
   `/api/gm?action=reload_config`；接口不可用时需重启服务端。玩家属性需重新登录刷新。
3. **客户端缓存**：武将属性、活动数据在登录时下发，改完要重新登录才可见。
4. **清单路径**：`manifest.json` 存相对仓库根路径，换机器/搬家后可复用；旧绝对路径仍兼容。
5. **ActivityShowConfig** 改的是客户端入口展示，必须 `ship` 重新打包；
   `activities.json` 不需要。
6. 生成的 Lua 为规范化输出（`env/_work/rt_all` 里会与 git HEAD 有格式差异，数据一致）。

## 8. 快照与回滚

每次 `build`/`ship` 在写文件前按选中的表生成快照，含 CSV、rt_all Lua，以及
（选中 activity 时）`activities.json`。`restore` 恢复 CSV/rt_all/JSON，但**不会**回滚工作簿，
继续编辑前先 `export` 重新生成 `config_src/*.xlsx`。

## 9. 常见问题

| 现象 | 处理 |
|---|---|
| `No module named openpyxl` | 用系统 Python 跑工具（`.workbuddy` 的解释器没有 openpyxl） |
| 服务端测试 GBK 编码报错 | 设 `PYTHONIOENCODING=utf-8`，并用 `.workbuddy` 的 Python（protobuf 兼容） |
| `preflight 缺少 schema/工作簿` | 按提示执行 `export`（或 `export --tables <分组>`） |
| 改完客户端没变化 | 客户端表需 `ship` 重打包；服务端数值需重新登录 |
| `config 模块缺失`（repack） | `manifest.json` 与 `build/config_gen/lua` 不同步，重新 `build` |
| 自检 `服务端解析成了 list` | 行主键被改成了连续 1..n，检查是否重排/重编号 |
