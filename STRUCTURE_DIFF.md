# R1 (hrattenddb) 与 V5 (yonbip_hr_tm) 表结构对比总结

对比时间：2026-09-24
源库连接：`dbproxy.diwork.com:12368` / `hrattenddb`
目标库连接：`dbproxy.diwork.com:12368` / `yonbip_hr_tm`

## 总体差异

| 表名 | 源字段数 | 目标字段数 | 仅源独有 | 仅目标独有 | 类型差异 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `ts_leave_apply` | 56 | 59 | 4 | 7 | 11 |
| `ts_leave_apply_detail` | 27 | 25 | 2 | 0 | 6 |
| `ts_leave_off_detail` | 41 | 54 | 3 | 16 | 10 |
| `ts_business_trip_apply` | 53 | 67 | 0 | 14 | 8 |
| `ts_business_trip_apply_detail` | 20 | 24 | 1 | 5 | 2 |
| `ts_business_trip_revoke_detail` | 35 | 50 | 1 | 16 | 4 |

## 处理策略

### 类型差异处理（自动 CAST）

| 源类型 | 目标类型 | 工具处理 |
| --- | --- | --- |
| `int(1)/int(4)` | `int(11)` | `CAST(... AS SIGNED)` |
| `tinyint(1)` | `int(11)` | `CAST(... AS SIGNED)` |
| `decimal(16,1)` | `decimal(16,2)` | `CAST(... AS DECIMAL(32,4))` |
| `varchar(36)` | `varchar(72)` | 字符串扩容，直接拷贝 |
| `varchar(500/512)` | `varchar(2048)` | 字符串扩容，直接拷贝 |
| `datetime` | `datetime` | 直接拷贝 |

### 源库独有字段（已跳过）

- `ts_leave_apply`: `hour_to_day, original_data, show_unit, target_data_precision`
- `ts_leave_apply_detail`: `hour_to_day, specify_send_staffid`
- `ts_leave_off_detail`: `hour_to_day, old_hour_to_day, specify_send_staffid`
- `ts_business_trip_apply`: 无
- `ts_business_trip_apply_detail`: `carbon_copy_staff_id`
- `ts_business_trip_revoke_detail`: `carbon_copy_staff_id`

### 目标库独有字段（自动补默认值）

| 表 | 默认字段 | 默认策略 |
| --- | --- | --- |
| `ts_leave_apply` | `bd_staffid, inner_leave_type_id, account_org_id, code, status, vouchdate, breastfeedLeaveDay` | `varchar`→`''`；`int`→`'0'`；`datetime`→`NULL` |
| `ts_leave_off_detail` | `orgid, leavetypeid, inner_leave_type_id, account_org_id, status, last_flag, filepath, code, vouchdate, old_code, old_filepath, old_remark, old_showbegindate, old_showenddate, breastfeedLeaveOffDay, oldBreastfeedLeaveDay` | 类型感知 |
| `ts_business_trip_apply` | `account_org_id, code, vouchdate, deptvid, orgvid, staffjobid, system_code, end_day_type, start_day_type, whole_day, showbegindate, showenddate, newstaffid, innertriptypeid` | 类型感知 |
| `ts_business_trip_apply_detail` | `end_day_type, start_day_type, whole_day, showbegindate, showenddate` | 类型感知 |
| `ts_business_trip_revoke_detail` | `code, vouchdate, applicant, orgid, end_day_type, start_day_type, last_flag, whole_day, showbegindate, showenddate, old_end_day_type, old_start_day_type, oldshowbegindate, oldshowenddate, newstaffid, innertriptypeid` | 类型感知 |

默认值可在 `config.py` → `DEFAULT_VALUES` 中调整，或通过 Web UI 「查看映射」手动编辑。

---

## 主键约定

所有 6 张表的主键均为 `id`（varchar(36) UUID），约定如下：

- 工具按 `id ASC` 顺序分批迁移，每次保存最大 `id` 作为下批起始
- 时间范围与 `id > last_pk` 一起作为查询条件
- 中断后重新启动会从已保存的 `last_pk` 继续

---

## 时间字段

| 表 | 字段 | 类型 | 备注 |
| --- | --- | --- | --- |
| `ts_leave_apply` | `leavebegintime` | datetime | 小写 |
| `ts_leave_apply_detail` | `LEAVEBEGINTIME` | datetime | 大写 |
| `ts_leave_off_detail` | `LEAVEOFFBEGINTIME` | datetime | 大写 |
| `ts_business_trip_apply` | `tripbegintime` | datetime | 小写 |
| `ts_business_trip_apply_detail` | `tripbegintime` | datetime | 小写 |
| `ts_business_trip_revoke_detail` | `tripbegintime` | datetime | 小写 |

工具按数据字典区分大小写精确查询时间范围。

---

## 推荐时间范围：2026 年之前

源库示例：最早 2023-03 至 2023-05，按 `leavebegintime/tripbegintime` 过滤：

```sql
SELECT COUNT(*) FROM ts_leave_apply
WHERE leavebegintime <= '2025-12-31 23:59:59';
```

工具默认上限是 `2025-12-31 23:59:59`，可通过 UI 时间控件调整。
