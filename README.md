# 请假出差数据迁移工具（R1 → V5）

将 R1 数据库 `hrattenddb` 中的请假 / 出差历史数据迁移到 V5 数据库 `yonbip_hr_tm`。

工具内置字段对比、智能默认值、分批迁移、断点重试、可视化进度。

---

## 功能特性

- **可视化 Web 界面**：浏览器管理数据库连接、字段映射、迁移任务
- **自动字段对比**：自动识别相同字段、目标库独有字段、V5 新增字段
- **智能默认值**：根据目标字段类型自动适配默认值（`varchar` 用 `''`，`decimal/int` 用 `0`，`datetime` 用 `NULL`）
- **断点重试**：按主键分批迁移，自动记录 `last_pk`，中断后再次启动会继续
- **重复主键跳过**：已存在数据自动跳过，不影响目标库原数据
- **实时进度**：每张表的总数 / 已迁 / 失败 / 当前断点
- **暂停控制**：当前批次完成后停止，不会破坏数据
- **完整迁移日志**：每批次记录耗时、范围、错误信息
- **类型自动转换**：`int(4)→int(11)`、`decimal(16,1)→decimal(16,2)` 等场景由 SQL `CAST` 处理
- **时间范围过滤**：每张表支持按各自时间字段设置范围（默认迁移 2026 年之前数据）

---

## 适用场景

将 R1（hrattenddb）的历史数据迁移到 V5（yonbip_hr_tm）：

| 表名 | 说明 | 默认时间字段 |
| --- | --- | --- |
| `ts_leave_apply` | 休假主表 | `leavebegintime` |
| `ts_leave_apply_detail` | 休假日明细 | `LEAVEBEGINTIME` |
| `ts_leave_off_detail` | 销假日明细 | `LEAVEOFFBEGINTIME` |
| `ts_business_trip_apply` | 出差主表 | `tripbegintime` |
| `ts_business_trip_apply_detail` | 出差明细 | `tripbegintime` |
| `ts_business_trip_revoke_detail` | 出差销差明细 | `tripbegintime` |

---

## 强制配置项

为避免误迁数据，启动迁移前 **必须** 完成以下配置：

1. **目标表名映射**：在「表管理 → 源-目标表名映射」为每张源表填写目标表名，**禁止与源表同名**。未配置时迁移会被拒绝。
2. **租户 ID 映射**：源表含 `tenantid` / `ytenant_id` 字段时，必须在「租户 ID 映射」配置映射；未在映射中的源行 **不会** 被迁移（通过 SQL `WHERE tenantid IN (...)` 过滤）。

## 字段差异处理要点

通过对比 `information_schema`，6 张表的字段情况：

- **整数列**：`int(1)/int(4)/tinyint` → `int(11)`，**完全兼容**。
- **Decimal 列**：`decimal(16,1)` → `decimal(16,2)`，由 SQL `CAST` 处理。
- **字符串长度变化**：`varchar(36) → varchar(72)`、`varchar(512) → varchar(2048)`，**兼容**。
- **源库独有字段**（目标库不存在）：`hour_to_day`、`original_data`、`show_unit`、`carbon_copy_staff_id`、`target_data_precision` 等 → **跳过**。
- **目标库独有字段**：`code`、`status`、`vouchdate`、`inner_leave_type_id`、`system_code`、`account_org_id`、`breastfeedLeaveDay/Day` → **按类型补默认**。

---

## 快速开始

### 1. 安装依赖

```bash
pip3 install -r requirements.txt
```

或在 macOS：

```bash
python3 -m pip install -r requirements.txt
```

### 2. 启动工具

```bash
./run.sh
```

或：

```bash
python3 app.py
```

启动后控制台打印：

```
==== 请假出差数据迁移工具 ====
启动: http://0.0.0.0:8765/
日志: /Users/shenzhen/Desktop/newcode/hrcloud-time/doc/data-migrate-tool/logs/migrate.log
```

打开浏览器访问 [http://localhost:8765](http://localhost:8765)。

### 3. Web 界面使用步骤

| 步骤 | 操作 | 说明 |
| --- | --- | --- |
| 1 | 「数据库配置」 → 填写源库 / 目标库连接 → 「保存配置」 | 密码会本地保存（SQLite 元数据库） |
| 2 | 「测试源库」「测试目标库」 | 验证连通性，显示版本与表数 |
| 3 | 「表管理」 → 点击每张表的「生成映射」 | 自动对比 R1/V5 字段，V5 独有字段给默认值 |
| 4 | 「表管理」 → 点击「查看」查看字段映射详情 | 标注：直接迁移 / 默认值 / 跳过 |
| 5 | 「表管理」 → 点击「启动」 | 配置批次大小（默认 500）与时间范围（默认 2025-12-31 23:59:59）|
| 6 | 「迁移进度」 查看实时进度 | 整体 + 每张表的进度条与断点 |
| 7 | 「迁移日志」 查看每个批次详情 | 包含主键范围、行数、错误信息 |
| 8 | 「重置」按钮可清空断点重新开始 | 已迁移数据保留 |

### 4. 断点重试

迁移中如遇异常退出 / 网络中断 / 主动暂停：

- 已迁移数据已提交到目标库（每子批 = 200 行 commit）
- 工具自动保存 `last_pk` 到 SQLite
- 重新到「表管理」点击「启动」，会从断点继续
- 想完全重跑：点击「重置」清空断点，再「启动」

---

## 配置说明

数据库连接配置存储在 `data/migrate_meta.db` 的 `db_config` 表，可通过 Web 界面修改，或用 SQLite 工具直接编辑：

```sql
sqlite3 data/migrate_meta.db
SELECT * FROM db_config;
```

字段含义：

| 键 | 示例 |
| --- | --- |
| `src_host` | `dbproxy.diwork.com` |
| `src_port` | `12368` |
| `src_db` | `hrattenddb` |
| `src_user` | `bip_hr_serv` |
| `src_password` | `***` |
| `dst_host` | `dbproxy.diwork.com` |
| `dst_port` | `12368` |
| `dst_db` | `yonbip_hr_tm` |
| `dst_user` | `bip_hr_serv` |
| `dst_password` | `***` |
| `page_size` | `500`（每批次拉取行数） |

---

## 自定义字段默认值

如某个目标库独有字段默认值不符合预期，可以：

1. 编辑 `config.py` 中 `DEFAULT_VALUES` 对应表的字典
2. 通过 Web 界面点击「生成映射」重写映射（会按你设置的默认值）
3. 或通过 API `/api/mapping` POST 提交自定义映射

`_coerce_default()` 函数会自动识别字段类型：

- `int/decimal/bit/float` → 默认值空时填 `'0'`
- `datetime/timestamp/date` → 默认值空时填 `NULL`
- 其它（varchar/char/text）→ 默认值空时填 `''`

---

## SQL 转换示例

以 `ts_business_trip_apply` 的 `cost` 字段为例，源库是 `decimal(16,1)`，目标库是 `decimal(16,2)`。工具生成的 SELECT 表达式为：

```sql
CAST(`cost` AS DECIMAL(32,4)) AS `cost`
```

整数列差异（如 `int(1) → int(11)`）也通过 `CAST(... AS SIGNED)` 兼容。

---

## 已知限制

1. **不处理业务校验**：主键重复跳过，其它错误计入 `failed` 并记录到日志，不会自动重试
2. **事务粒度**：每 200 行 commit 一次，目标库单批失败不会回滚已写入数据
3. **应用层延迟**：通过 DIWORK dbproxy，单批拉取可能因网络而慢
4. **同时只能迁移一张表**：每张表单独启动，不支持并发
5. **目标库字段默认值** 仅供参考，请按业务实际需求调整 `DEFAULT_VALUES`

---

## 目录结构

```
data-migrate-tool/
├── app.py              # Flask Web 入口（API + 页面）
├── config.py           # 全局配置：表清单、时间字段、默认值
├── db.py               # 数据库工具：MySQL 连接、SQLite 元数据
├── migrate_engine.py   # 迁移引擎核心：字段对比、断点续传、批量执行
├── requirements.txt    # Python 依赖
├── run.sh              # 启动脚本
├── README.md           # 本文件
├── data/
│   └── migrate_meta.db # SQLite（元数据：连接配置 / 字段映射 / 进度 / 日志）
├── logs/
│   └── migrate.log     # 运行日志
├── static/css/         # 自定义 CSS
└── templates/          # HTML 模板
```

---

## API 速查

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/` | 概览页 |
| GET | `/config` | 数据库配置页 |
| GET | `/tables` | 表管理页 |
| GET | `/progress` | 迁移进度页 |
| GET | `/logs` | 迁移日志页 |
| GET/POST | `/api/config` | 获取 / 保存配置 |
| POST | `/api/test` | 测试连接 |
| POST/GET | `/api/mapping/{table}` | 生成 / 查看字段映射 |
| POST | `/api/mapping` | 批量保存字段映射 |
| POST | `/api/migrate` | 启动一张表的迁移 |
| POST | `/api/migrate/stop` | 暂停（断点重试安全）|
| GET | `/api/progress` | 当前所有任务进度 |
| POST | `/api/progress/{table}` | 重新统计行数 |
| POST | `/api/reset/{table}` | 重置单表进度 |
| POST | `/api/reset_all` | 重置全部 |
| GET | `/api/logs` | 查询日志 |

---

## 推荐部署

1. **生产环境**使用 gunicorn：

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:8765 app:app
```

2. **作为后台服务**：

```bash
nohup ./run.sh > logs/console.log 2>&1 &
```

3. **跨主机访问**：把 `HOST = "0.0.0.0"` 与防火墙规则匹配。

---

## 打包为独立可执行文件（macOS / Windows / Linux）

通过 PyInstaller 把工具打包成单文件可执行，**目标机器无需安装 Python**。

### 快速构建

```bash
# macOS / Linux
./tools/build.sh

# Windows（双击即可）
tools\build.bat
```

产物：

| 平台 | 路径 |
| --- | --- |
| macOS   | `dist/hrcloud-migrate` |
| Windows | `dist/hrcloud-migrate.exe` |
| Linux   | `dist/hrcloud-migrate` |

### 自定义参数

```bash
# 输出目录模式（启动快；包含多个 .so/.pyd 文件）
python3 tools/build.py --onedir

# 跳过 pyinstaller 自动安装
python3 tools/build.py --no-install
```

### GitHub Actions 跨平台构建

`.github/workflows/build.yml` 已配置 macOS / Windows / Ubuntu 矩阵构建：

- **手动触发**：`Actions → Build Standalone Executables → Run workflow`
- **推送 tag 自动发布**：`git tag v1.0.0 && git push --tags` → 自动构建并上传到 GitHub Release

### 分发与运行

将产物文件拷贝到目标机器直接运行：

- **首次运行**会自动在可执行文件旁创建 `data/`、`logs/` 目录
- **不会**污染系统 Python 环境
- 浏览器**自动打开** `http://localhost:8765/`（仅打包版本）

### 注意事项

- **每个平台需单独构建**：PyInstaller 不支持交叉编译。在 macOS 上构建 macOS 版，在 Windows 上构建 Windows 版（或用 CI）。
- **文件大小**：约 25-40 MB（包含完整 Python 解释器 + Flask + pymysql）。
- **首次启动**：onefile 模式会解压到临时目录，启动慢 1-2 秒。如对启动速度敏感，用 `--onedir`。
- **防病毒软件**：PyInstaller 打包的 exe 偶尔会被 Windows Defender 误报，需手动加白。

---

## License

内部工具，仅供 hrcloud-time 项目使用。
