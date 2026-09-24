"""
数据迁移引擎 - 核心实现
- 自动对比源/目标表结构
- 生成字段映射（相同字段直接映射，仅目标有字段给默认值）
- 分批迁移 + 断点续传
- 处理 decimal 精度等类型转换
"""
import decimal
import json
import logging
import re
import threading
import time
from datetime import datetime

from db import (
    init_sqlite, get_all_configs, get_mysql_columns,
    get_pk_column, mysql_cursor, open_mysql,
    upsert_field_mapping, get_field_mappings,
    get_dst_table, get_tenant_map_dict, scan_source_tenants,
    get_task, upsert_task, write_log, recent_logs,
    sqlite_cursor,
)
from config import (
    TABLES, TABLE_TIME_FIELDS, TABLE_PRIMARY_KEYS, DEFAULT_VALUES,
    DEFAULT_BATCH_SIZE, SUB_BATCH_SIZE, TENANT_FIELDS,
)

log = logging.getLogger("migrate")

# 任务状态常量
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_ERROR = "error"
ALL_STATUSES = {STATUS_PENDING, STATUS_RUNNING, STATUS_PAUSED, STATUS_DONE, STATUS_ERROR}

# 全局任务状态：内存中保存当前运行状态
_STATE = {"running": False, "current_table": None, "thread": None, "cancel": False}
_STATE_LOCK = threading.Lock()


# ============================================================
# 字段映射自动对比
# ============================================================

def _normalize_type(t):
    """归一化类型字符串，去掉长度/精度，便于同名比较"""
    if not t:
        return ""
    s = t.lower().strip()
    # int 宽度不影响范围
    s = re.sub(r"int\(\d+\)", "int", s)
    # decimal 不同精度单独保留，但兼容比较常见 1==2 在迁移时常忽略
    return s


def _is_compatible(src_type, dst_type):
    """判断两列类型是否兼容（可直接拷贝）"""
    if not src_type:
        return False
    a, b = _normalize_type(src_type), _normalize_type(dst_type)
    if a == b:
        return True
    # 兼容常见精度差异（int/tinyint/smallint/mediumint）
    int_families = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}
    if a in int_families and b in int_families:
        return True
    # char/varchar/text/blob 互转
    text_families = {"char", "varchar", "text", "mediumtext", "longtext"}
    if a in text_families and b in text_families:
        return True
    # datetime/timestamp
    dt_families = {"datetime", "timestamp", "date"}
    if a in dt_families and b in dt_families:
        return True
    # decimal 一律可转 decimal，精度差异由 CAST 处理
    if a.startswith("decimal") and b.startswith("decimal"):
        return True
    return False


def _coerce_default(dst_type, raw):
    """根据目标字段类型适配默认值（空字符串必须变成数值类的 0）"""
    if raw is None or raw == "":
        t = (dst_type or "").lower()
        if t.startswith("int") or t.startswith("tinyint") or t.startswith("smallint") \
           or t.startswith("mediumint") or t.startswith("bigint") \
           or t.startswith("decimal") or t.startswith("numeric") \
           or t.startswith("float") or t.startswith("double") \
           or t.startswith("bit"):
            return "0"
        if t.startswith("date") or t.startswith("timestamp") or t.startswith("datetime"):
            return None
        return ""
    # 非空值：保持原样
    return raw


def build_field_mapping(src_cols, dst_cols, table):
    """
    对比两表字段，生成映射：
      (dst_field, src_field or None, default_value)
    """
    src_map = {c["COLUMN_NAME"]: c for c in src_cols}
    dst_map = {c["COLUMN_NAME"]: c for c in dst_cols}

    mappings = []
    notes = {"src_only": [], "type_diff": [], "compatible": 0}

    defaults = DEFAULT_VALUES.get(table, {})

    for dst_name, dst_col in dst_map.items():
        if dst_name in src_map:
            src_col = src_map[dst_name]
            if _is_compatible(src_col["COLUMN_TYPE"], dst_col["COLUMN_TYPE"]):
                mappings.append((dst_name, dst_name, None))
                notes["compatible"] += 1
            else:
                notes["type_diff"].append(
                    f"{dst_name}: src={src_col['COLUMN_TYPE']} dst={dst_col['COLUMN_TYPE']}"
                )
                mappings.append((dst_name, dst_name, None))
        else:
            # 目标独有字段
            raw_default = defaults.get(dst_name, "")
            coerced = _coerce_default(dst_col["COLUMN_TYPE"], raw_default)
            mappings.append((dst_name, None, coerced))

    for src_name in src_map:
        if src_name not in dst_map:
            notes["src_only"].append(src_name)

    return mappings, notes


def compute_and_store_mapping(host_src, port_src, user_src, pwd_src, db_src,
                              host_dst, port_dst, user_dst, pwd_dst, db_dst,
                              table, dst_table=None):
    """计算一张表的字段映射并入库，返回映射+备注
    src 端用 table 取列；dst 端用 dst_table 取列（默认等于 table）"""
    src_cols = get_mysql_columns(host_src, port_src, user_src, pwd_src, db_src, table)
    dst_table = dst_table or table
    dst_cols = get_mysql_columns(host_dst, port_dst, user_dst, pwd_dst, db_dst, dst_table)

    if not src_cols:
        raise RuntimeError(f"源表 {table} 不存在或无权限")
    if not dst_cols:
        raise RuntimeError(f"目标表 {dst_table} 不存在或无权限")

    mappings, notes = build_field_mapping(src_cols, dst_cols, table)
    upsert_field_mapping(table, mappings)
    return mappings, notes


# ============================================================
# SQL 构建
# ============================================================

def _sql_val(v):
    """把 python 值渲染成 SQL 字面量"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float, decimal.Decimal)):
        return str(v)
    if isinstance(v, (datetime,)):
        return f"'{v.strftime('%Y-%m-%d %H:%M:%S')}'"
    s = str(v).replace("'", "''")
    return f"'{s}'"


def _cast_expr(src_field, src_type, dst_type):
    """根据两端类型生成 SELECT 表达式"""
    if not src_field:
        return "NULL"
    if _normalize_type(src_type) == _normalize_type(dst_type):
        # 完全相同，直接用
        return f"`{src_field}`"
    # 整型族强转
    int_families = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}
    if _normalize_type(src_type) in int_families and _normalize_type(dst_type) in int_families:
        return f"CAST(`{src_field}` AS SIGNED)"
    # decimal 精度对齐
    if _normalize_type(src_type).startswith("decimal") and _normalize_type(dst_type).startswith("decimal"):
        return f"CAST(`{src_field}` AS DECIMAL(32,4))"
    # datetime / timestamp 互转
    dt_families = {"datetime", "timestamp", "date"}
    if _normalize_type(src_type) in dt_families and _normalize_type(dst_type) in dt_families:
        return f"CAST(`{src_field}` AS {dst_type.upper().split('(')[0]})"
    # 字符串相似，直接返回
    return f"`{src_field}`"


def _build_time_range_and_pk_where(time_field, time_from, time_to,
                                    last_pk=None, pk_field="id"):
    """构造时间范围 + last_pk 的 WHERE 子句与参数，返回 (clauses, params)"""
    clauses, params = [], []
    if time_field and (time_from or time_to):
        if time_from:
            clauses.append(f"`{time_field}` >= %s")
            params.append(time_from)
        if time_to:
            clauses.append(f"`{time_field}` <= %s")
            params.append(time_to)
    if last_pk:
        clauses.append(f"`{pk_field}` > %s")
        params.append(last_pk)
    return clauses, params


def _tenant_where(tenant_field, tenant_map):
    """构造租户 IN (...) 子句与参数；返回 (clause, params)，未启用则返回 (None, [])"""
    if not (tenant_field and tenant_map):
        return None, []
    src_tenants = list(tenant_map.keys())
    placeholders = ",".join(["%s"] * len(src_tenants))
    return f"`{tenant_field}` IN ({placeholders})", src_tenants


def _fetch_existing_pks(dst_cur, dst_table, pk_field, candidate_pks, chunk_size=500):
    """从目标表查询 candidate_pks 中已存在的主键集合，分批避免 IN 列表过大"""
    existing = set()
    if not candidate_pks:
        return existing
    for i in range(0, len(candidate_pks), chunk_size):
        chunk = candidate_pks[i:i + chunk_size]
        placeholders = ",".join(["%s"] * len(chunk))
        dst_cur.execute(
            f"SELECT `{pk_field}` FROM `{dst_table}` WHERE `{pk_field}` IN ({placeholders})",
            chunk
        )
        existing.update(r[pk_field] for r in dst_cur.fetchall())
    return existing


# ============================================================
# 主迁移流程
# ============================================================

def _with_pk_aware_select(mappings, src_cols_map, dst_cols_map):
    """为映射增加 CAST 处理；对租户 ID 字段套 CASE WHEN 替换"""
    tenant_map = get_tenant_map_dict() if TENANT_FIELDS else {}
    select_parts = []
    used_tenant = False
    for (dst_field, src_field, default_value) in mappings:
        if src_field is None:
            select_parts.append(f"{_sql_val(default_value)} AS `{dst_field}`")
            continue
        src_type = src_cols_map.get(src_field, {}).get("COLUMN_TYPE", "")
        dst_type = dst_cols_map.get(dst_field, {}).get("COLUMN_TYPE", "")
        expr = _cast_expr(src_field, src_type, dst_type)
        # 租户 ID 字段（tenantid / ytenant_id）：套 CASE WHEN 映射
        if tenant_map and src_field.lower() in TENANT_FIELDS:
            expr = _wrap_tenant_id_case(src_field, tenant_map)
            used_tenant = True
        select_parts.append(expr + f" AS `{dst_field}`")
    if used_tenant and tenant_map:
        log.info("[%s] 已启用租户映射: %d 条", "select", len(tenant_map))
    return select_parts


def _wrap_tenant_id_case(src_field, tenant_map):
    """把 `col` 包装成 CASE WHEN 替换后的表达式
    tenant_map: {src_tenantid: dst_tenantid}
    """
    if not tenant_map:
        return f"`{src_field}`"
    safe_map = [(s.replace("\\", "\\\\").replace("'", "''"),
                 d.replace("\\", "\\\\").replace("'", "''"))
                for s, d in tenant_map.items()]
    whens = " ".join(f"WHEN '{s}' THEN '{d}'" for s, d in safe_map)
    return f"CASE `{src_field}` {whens} ELSE `{src_field}` END"


def migrate_table(table, page_size=None, time_from=None, time_to=None,
                  source=None, target=None, only_count=False, dst_table=None):
    """
    迁移一张表，断点续传由 upsert_task 维护
    table: 源表名（同时也是任务/映射/日志的 key）
    dst_table: 目标表名；None 时从 db_config 映射中查找，未配置则与源表同名
    """
    cfg = get_all_configs()
    if not source:
        source = {
            "host": cfg.get("src_host"),
            "port": int(cfg.get("src_port", 3306)),
            "user": cfg.get("src_user"),
            "password": cfg.get("src_password"),
            "db": cfg.get("src_db"),
        }
    if not target:
        target = {
            "host": cfg.get("dst_host"),
            "port": int(cfg.get("dst_port", 3306)),
            "user": cfg.get("dst_user"),
            "password": cfg.get("dst_password"),
            "db": cfg.get("dst_db"),
        }
    page_size = int(page_size or cfg.get("page_size") or DEFAULT_BATCH_SIZE)

    # 时间范围允许每次覆盖
    task = get_task(table) or {}
    if time_from is None and time_to is None:
        time_from = task.get("time_from") or ""
        time_to = task.get("time_to") or ""

    # 解析目标表名（必须显式配置，不允许与源表同名）
    dst_table = dst_table or get_dst_table(table)
    if not dst_table:
        raise RuntimeError(
            f"表 {table} 目标表未配置，请先在「表管理」配置源-目标表名映射"
        )
    if dst_table == table:
        raise RuntimeError(
            f"表 {table} 目标表与源表同名，禁止迁移"
        )
    log.info("[%s] 目标表映射: %s -> %s", table, table, dst_table)

    # 字段映射与列信息
    mappings = get_field_mappings(table)
    if not mappings:
        raise RuntimeError(f"表 {table} 尚未生成字段映射，请先在「表管理」点击生成")

    # 取源/目标列定义（用于 CAST）。源用 src_table，目标用 dst_table
    src_cols = get_mysql_columns(**source, table=table)
    dst_cols = get_mysql_columns(**target, table=dst_table)
    src_cols_map = {c["COLUMN_NAME"]: c for c in src_cols}
    dst_cols_map = {c["COLUMN_NAME"]: c for c in dst_cols}
    pk_field = get_pk_column(**source, table=table)

    # 校验租户映射：源表含租户字段（tenantid / ytenant_id）则映射必须非空
    src_tenant_field = None
    for c in src_cols:
        if c["COLUMN_NAME"].lower() in TENANT_FIELDS:
            src_tenant_field = c["COLUMN_NAME"]
            break
    tenant_map = get_tenant_map_dict() if (TENANT_FIELDS and src_tenant_field) else {}
    if src_tenant_field and not tenant_map:
        raise RuntimeError(
            f"表 {table} 含租户字段 {src_tenant_field}，必须先在「租户 ID 映射」配置映射"
        )
    tenant_clause, tenant_params = _tenant_where(src_tenant_field, tenant_map)

    # 取目标表主键列名（用于跨库存在性检查；通常与源表 PK 同名）
    dst_pk_field = get_pk_column(**target, table=dst_table) or pk_field

    # 阶段1：先统计"未迁移"总数（src 候选 PK 与 dst PK 求差集）
    src_conn = open_mysql(**source)
    dst_conn = open_mysql(**target)
    try:
        with src_conn.cursor() as src_cur, dst_conn.cursor() as dst_cur:
            # 取源端匹配条件的主键（避免一次性拉全表行）
            tf = TABLE_TIME_FIELDS.get(table)
            cnt_clauses, cnt_params = _build_time_range_and_pk_where(
                tf, time_from, time_to, last_pk=None, pk_field=pk_field)
            if tenant_clause:
                cnt_clauses.append(tenant_clause)
                cnt_params.extend(tenant_params or [])
            src_id_sql = f"SELECT `{pk_field}` FROM `{table}`"
            if cnt_clauses:
                src_id_sql += " WHERE " + " AND ".join(cnt_clauses)
            src_cur.execute(src_id_sql, cnt_params)
            src_pks = [r[pk_field] for r in src_cur.fetchall()]

            # 在目标表里查已存在的主键（分批 IN）
            existing_pks = _fetch_existing_pks(dst_cur, dst_table, dst_pk_field, src_pks)
            total = sum(1 for pk in src_pks if pk not in existing_pks)

        # 检查断点
        last_pk = task.get("last_pk")
        migrated = task.get("migrated", 0) or 0
        failed = task.get("failed", 0) or 0
        started_at = task.get("started_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prev_status = task.get("status") or STATUS_PENDING

        _persist_task(table, status=STATUS_RUNNING, total=total, migrated=migrated,
                      failed=failed, last_pk=last_pk, page_size=page_size,
                      time_from=time_from or "", time_to=time_to or "",
                      started_at=started_at, finished_at=None, err_msg="")

        if only_count:
            _persist_task(table, status=prev_status, total=total, migrated=migrated,
                          failed=failed, last_pk=last_pk, page_size=page_size,
                          time_from=time_from or "", time_to=time_to or "",
                          started_at=started_at)
            return {"total": total, "migrated": migrated, "failed": failed}

        # 所有源记录均已存在于目标表，无需迁移
        if total == 0:
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _persist_task(table, status=STATUS_DONE, total=0, migrated=0, failed=0,
                          last_pk=None, page_size=page_size,
                          time_from=time_from or "", time_to=time_to or "",
                          started_at=started_at, finished_at=finished_at,
                          err_msg="所有记录已存在于目标表，无需迁移")
            log.info("[%s] 所有源记录均已存在于目标表，无需迁移", table)
            return {"total": 0, "migrated": 0, "failed": 0,
                    "last_pk": "", "no_need": True,
                    "msg": "无需迁移：所有源记录均已存在于目标表"}

        # 阶段2：分批迁移
        return _run_batch_loop(table, source, target, dst_table, mappings, src_cols_map,
                               dst_cols_map, pk_field, page_size, time_from,
                               time_to, last_pk, migrated, failed, started_at,
                               total, src_conn, dst_conn,
                               tenant_clause=tenant_clause, tenant_params=tenant_params,
                               dst_pk_field=dst_pk_field)
    finally:
        src_conn.close()
        dst_conn.close()


def _persist_task(table, status=None, total=None, migrated=None, failed=None,
                  last_pk=None, page_size=None, time_from="", time_to="",
                  started_at=None, finished_at=None, err_msg=""):
    """写入 migrate_task 记录；None 表示保持原值。"""
    fields = {
        "status": status,
        "total": total,
        "migrated": migrated,
        "failed": failed,
        "last_pk": last_pk,
        "page_size": page_size,
        "time_field": TABLE_TIME_FIELDS.get(table, ""),
        "time_from": time_from,
        "time_to": time_to,
        "started_at": started_at,
        "finished_at": finished_at,
        "err_msg": err_msg,
    }
    final = {k: v for k, v in fields.items() if v is not None}
    upsert_task(table, table_name=table, **final)


def _run_batch_loop(table, source, target, dst_table, mappings, src_cols_map, dst_cols_map,
                    pk_field, page_size, time_from, time_to, last_pk,
                    migrated, failed, started_at, total, src_conn, dst_conn,
                    tenant_clause=None, tenant_params=None, dst_pk_field=None):
    """分批迁移主循环"""
    # SELECT 表达式（含 CAST 与默认值），头部带主键字段
    select_exprs = _with_pk_aware_select(mappings, src_cols_map, dst_cols_map)
    select_exprs.insert(0, f"`{pk_field}` AS `{pk_field}`")

    # INSERT 列定义 - 写入到目标表（可能与源表不同名）
    insert_cols = [m[0] for m in mappings]
    insert_cols_sql = "`" + "`,`".join(insert_cols) + "`"
    insert_template = (f"INSERT INTO `{dst_table}` ({insert_cols_sql}) VALUES "
                       + "(" + ",".join(["%s"] * len(insert_cols)) + ")")
    default_only = {m[0]: m[2] for m in mappings if m[1] is None}

    def _persist_running():
        _persist_task(table, status=STATUS_RUNNING, total=total, migrated=migrated,
                      failed=failed, last_pk=str(last_pk or ""), page_size=page_size,
                      time_from=time_from or "", time_to=time_to or "",
                      started_at=started_at, finished_at=None, err_msg="")

    tf = TABLE_TIME_FIELDS.get(table)
    time_field = tf if (time_from or time_to) else None
    check_pk_field = dst_pk_field or pk_field

    batch_no = 0
    while True:
        if is_cancel_requested():
            log.info("[%s] 已收到暂停请求，停止当前批次（断点已记录）", table)
            _persist_task(table, status=STATUS_PAUSED, total=total, migrated=migrated,
                          failed=failed, last_pk=str(last_pk or ""),
                          page_size=page_size,
                          time_from=time_from or "", time_to=time_to or "",
                          started_at=started_at, finished_at=None, err_msg="")
            break

        where_clauses, params = _build_time_range_and_pk_where(
            time_field, time_from, time_to, last_pk, pk_field)
        if tenant_clause:
            where_clauses.append(tenant_clause)
            params.extend(tenant_params or [])
        sel_sql = f"SELECT {','.join(select_exprs)} FROM `{table}`"  # 源端用 src_table
        if where_clauses:
            sel_sql += " WHERE " + " AND ".join(where_clauses)
        sel_sql += f" ORDER BY `{pk_field}` ASC LIMIT {page_size}"

        log.debug("[%s] %s", table, sel_sql)
        batch_no += 1
        batch_start = datetime.now()
        try:
            with src_conn.cursor() as src_cur:
                src_cur.execute(sel_sql, params)
                rows = src_cur.fetchall()
            if not rows:
                break

            # 过滤掉目标表已存在的主键（避免重复插入）
            batch_pks = [r[pk_field] for r in rows]
            with dst_conn.cursor() as dst_cur:
                existing_pks = _fetch_existing_pks(
                    dst_cur, dst_table, check_pk_field, batch_pks)

            insert_payload = []
            new_pk = last_pk
            skipped_in_batch = 0
            for row in rows:
                if row[pk_field] in existing_pks:
                    skipped_in_batch += 1
                    new_pk = row[pk_field]
                    continue
                vals = [_normalize_value(row.get(col))
                        if col in row else
                        (default_only.get(col) if col in default_only else None)
                        for col in insert_cols]
                insert_payload.append(tuple(vals))
                new_pk = row[pk_field]

            if not insert_payload:
                # 整批均已存在于目标表，仅前进断点
                last_pk = batch_pks[-1]
                write_log(table, batch_no,
                          str(last_pk) if last_pk else "",
                          str(last_pk) if last_pk else "",
                          0, True,
                          batch_start.strftime("%Y-%m-%d %H:%M:%S"),
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          f"整批 {len(rows)} 行已存在，跳过")
                _persist_running()
                log.info("[%s] batch %d: 全部 %d 行已在目标表，跳过 (pk=%s)",
                         table, batch_no, len(rows), last_pk)
                if len(rows) < page_size:
                    break
                continue

            inserted_this_batch, failed_this_batch, err_msg = _write_chunk(
                dst_conn, insert_template, insert_payload)

            migrated += inserted_this_batch
            failed += failed_this_batch
            last_pk = new_pk
            write_log(table, batch_no,
                      str(last_pk) if last_pk else "",
                      str(last_pk) if last_pk else "",
                      inserted_this_batch,
                      success=(failed_this_batch == 0),
                      start_at=batch_start.strftime("%Y-%m-%d %H:%M:%S"),
                      end_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      err_msg=err_msg if failed_this_batch else "")
            _persist_running()
            log.info("[%s] batch %d: rows=%d inserted=%d skipped=%d failed=%d pk=%s",
                     table, batch_no, len(rows), inserted_this_batch,
                     skipped_in_batch, failed_this_batch, last_pk)
            if len(rows) < page_size:
                break
        except Exception as e:
            err = str(e)[:500]
            failed += page_size
            write_log(table, batch_no,
                      str(last_pk or ""), str(last_pk or ""),
                      0, False,
                      batch_start.strftime("%Y-%m-%d %H:%M:%S"),
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      err)
            _persist_running()
            log.exception("[%s] batch %d 失败: %s", table, batch_no, err)
            break

    end_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _persist_task(table, status=STATUS_DONE, total=total, migrated=migrated,
                  failed=failed, last_pk=str(last_pk or ""),
                  page_size=page_size,
                  time_from=time_from or "", time_to=time_to or "",
                  started_at=started_at, finished_at=end_at, err_msg="")
    return {"total": total, "migrated": migrated, "failed": failed,
            "last_pk": str(last_pk or "")}


def _normalize_value(v):
    """把 datetime / Decimal 转成可被 pymysql 序列化的 Python 值"""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, decimal.Decimal):
        return str(v)
    return v


def _write_chunk(dst_conn, insert_template, payload):
    """单批写入：根据执行结果返回 (inserted, failed, err_msg)"""
    sub = SUB_BATCH_SIZE
    dst_cur = dst_conn.cursor()
    inserted, failed, err_msg = 0, 0, ""
    for i in range(0, len(payload), sub):
        chunk = payload[i:i + sub]
        try:
            dst_cur.executemany(insert_template, chunk)
            dst_conn.commit()
            inserted += len(chunk)
        except Exception as e:
            dst_conn.rollback()
            err_msg = str(e)[:500]
            # 回退逐行插入：主键重复跳过，其他错误计入 failed
            dst_cur = dst_conn.cursor()
            for t in chunk:
                try:
                    dst_cur.execute(insert_template, t)
                    inserted += 1
                except Exception as ie:
                    es = str(ie).lower()
                    if "duplicate" in es or "1062" in es:
                        continue
                    failed += 1
                    log.warning("[single-row fail] %s", ie)
            dst_conn.commit()
    return inserted, failed, err_msg


def _runner(table, page_size, time_from, time_to):
    with _STATE_LOCK:
        _STATE["running"] = True
        _STATE["current_table"] = table
        _STATE["cancel"] = False
    try:
        migrate_table(table, page_size=page_size, time_from=time_from, time_to=time_to)
    except Exception as e:
        err = str(e)[:500]
        log.exception("迁移任务异常: %s", e)
        # 把 task 状态标记为 error，便于 UI 显示
        try:
            task = get_task(table) or {}
            _persist_task(table, status=STATUS_ERROR,
                          total=task.get("total"),
                          migrated=task.get("migrated"),
                          failed=task.get("failed"),
                          last_pk=task.get("last_pk"),
                          page_size=task.get("page_size") or page_size,
                          time_from=task.get("time_from") or time_from or "",
                          time_to=task.get("time_to") or time_to or "",
                          started_at=task.get("started_at"),
                          finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          err_msg=err)
        except Exception:
            log.exception("写入 error 状态失败")
    finally:
        with _STATE_LOCK:
            _STATE["running"] = False
            _STATE["current_table"] = None
            _STATE["thread"] = None
            _STATE["cancel"] = False


def start_migration(table, page_size, time_from, time_to):
    """启动后台线程迁移一张表"""
    with _STATE_LOCK:
        if _STATE["running"]:
            return False, "已有迁移任务在运行"
        t = threading.Thread(target=_runner, args=(table, page_size, time_from, time_to),
                             daemon=True)
        _STATE["thread"] = t
        t.start()
        return True, f"已启动 {table} 迁移"


def request_cancel():
    """请求取消当前迁移（标记当前批次完成后停止）"""
    with _STATE_LOCK:
        _STATE["cancel"] = True
    return _STATE["running"]


def is_cancel_requested():
    with _STATE_LOCK:
        return _STATE.get("cancel", False)


def get_state():
    with _STATE_LOCK:
        return {
            "running": _STATE["running"],
            "current_table": _STATE["current_table"],
            "alive": _STATE["thread"].is_alive() if _STATE["thread"] else False,
        }
