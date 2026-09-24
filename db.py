"""
数据库工具：MySQL 连接管理 + 元数据 SQLite 持久化
"""
import json
import sqlite3
import threading
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from config import SQLITE_PATH


# ============================================================
# SQLite（元数据 / 进度 / 日志 / 字段映射）
# ============================================================
_sqlite_lock = threading.Lock()


def get_sqlite_conn():
    conn = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def sqlite_cursor():
    """线程安全的 sqlite 游标上下文"""
    with _sqlite_lock:
        conn = get_sqlite_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_sqlite():
    """初始化迁移元数据库表结构"""
    with sqlite_cursor() as conn:
        # 启用 WAL + busy_timeout，多线程并发读写更稳定
        cur_exec = conn.cursor()
        cur_exec.execute("PRAGMA journal_mode=WAL")
        cur_exec.execute("PRAGMA busy_timeout=5000")
        cur_exec.execute("PRAGMA synchronous=NORMAL")
        cur_exec.close()
        cur = conn.cursor()
        # 全局连接配置（一条记录）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS db_config (
                k TEXT PRIMARY KEY,
                v TEXT
            )
        """)
        # 表迁移任务：每张表一条记录
        cur.execute("""
            CREATE TABLE IF NOT EXISTS migrate_task (
                table_name TEXT PRIMARY KEY,
                status TEXT,
                total INTEGER,
                migrated INTEGER,
                failed INTEGER,
                last_pk TEXT,
                page_size INTEGER,
                time_field TEXT,
                time_from TEXT,
                time_to TEXT,
                started_at TEXT,
                finished_at TEXT,
                err_msg TEXT
            )
        """)
        # 字段映射：每张表每个目标字段
        cur.execute("""
            CREATE TABLE IF NOT EXISTS table_name_map (
                src_table TEXT PRIMARY KEY,
                dst_table TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenant_id_map (
                src_tenantid TEXT PRIMARY KEY,
                dst_tenantid TEXT,
                updated_at TEXT
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_tenant_dst ON tenant_id_map(dst_tenantid)"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS field_mapping (
                table_name TEXT,
                dst_field TEXT,
                src_field TEXT,
                default_value TEXT,
                PRIMARY KEY (table_name, dst_field)
            )
        """)
        # 迁移日志（按批次）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS migrate_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT,
                batch_no INTEGER,
                from_pk TEXT,
                to_pk TEXT,
                row_count INTEGER,
                success INTEGER,
                start_at TEXT,
                end_at TEXT,
                err_msg TEXT
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_table ON migrate_log(table_name)
        """)


def set_config(k, v):
    with sqlite_cursor() as conn:
        conn.cursor().execute(
            "INSERT OR REPLACE INTO db_config(k,v) VALUES(?,?)", (k, v)
        )


def get_config(k, default=None):
    with sqlite_cursor() as conn:
        row = conn.cursor().execute(
            "SELECT v FROM db_config WHERE k=?", (k,)
        ).fetchone()
        return row["v"] if row else default


def get_all_configs():
    with sqlite_cursor() as conn:
        rows = conn.cursor().execute("SELECT k,v FROM db_config").fetchall()
        return {r["k"]: r["v"] for r in rows}


def upsert_task(table, **fields):
    """插入或更新任务记录"""
    keys = list(fields.keys())
    cols = ["table_name"] + keys
    placeholders = ",".join(["?"] * len(cols))
    update_clause = ",".join([f"{k}=excluded.{k}" for k in keys])
    with sqlite_cursor() as conn:
        conn.cursor().execute(
            f"INSERT INTO migrate_task ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(table_name) DO UPDATE SET {update_clause}",
            [table] + [fields[k] for k in keys]
        )


def get_task(table):
    with sqlite_cursor() as conn:
        row = conn.cursor().execute(
            "SELECT * FROM migrate_task WHERE table_name=?", (table,)
        ).fetchone()
        return dict(row) if row else None


def list_tasks():
    with sqlite_cursor() as conn:
        rows = conn.cursor().execute(
            "SELECT * FROM migrate_task ORDER BY table_name"
        ).fetchall()
        return [dict(r) for r in rows]


def write_log(table, batch_no, from_pk, to_pk, count, success, start_at, end_at, err_msg=""):
    with sqlite_cursor() as conn:
        conn.cursor().execute(
            """INSERT INTO migrate_log(table_name,batch_no,from_pk,to_pk,row_count,success,
            start_at,end_at,err_msg) VALUES(?,?,?,?,?,?,?,?,?)""",
            (table, batch_no, from_pk, to_pk, count, 1 if success else 0,
             start_at, end_at, err_msg)
        )


def recent_logs(table=None, limit=50):
    with sqlite_cursor() as conn:
        cur = conn.cursor()
        if table:
            rows = cur.execute(
                "SELECT * FROM migrate_log WHERE table_name=? "
                "ORDER BY id DESC LIMIT ?", (table, limit)
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT * FROM migrate_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def upsert_field_mapping(table, mappings):
    """mappings: [(dst_field, src_field, default_value)]"""
    with sqlite_cursor() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM field_mapping WHERE table_name=?", (table,))
        cur.executemany(
            "INSERT INTO field_mapping(table_name,dst_field,src_field,default_value) "
            "VALUES(?,?,?,?)",
            [(table, d, s, v) for (d, s, v) in mappings]
        )


def get_table_name_map():
    """返回 {src_table: dst_table}，未配置时返回空 dict"""
    with sqlite_cursor() as conn:
        rows = conn.cursor().execute(
            "SELECT src_table, dst_table FROM table_name_map"
        ).fetchall()
        return {r["src_table"]: r["dst_table"] for r in rows}


def get_dst_table(src_table):
    """获取源表对应的目标表名；未配置时返回 None（必须显式配置）"""
    with sqlite_cursor() as conn:
        row = conn.cursor().execute(
            "SELECT dst_table FROM table_name_map WHERE src_table=?",
            (src_table,)
        ).fetchone()
        if not row or not row["dst_table"]:
            return None
        return row["dst_table"]


def set_table_name_map(src_table, dst_table):
    """设置源-目标表名映射；dst_table 与 src_table 相同时删除映射"""
    with sqlite_cursor() as conn:
        cur = conn.cursor()
        if not dst_table or dst_table == src_table:
            cur.execute("DELETE FROM table_name_map WHERE src_table=?", (src_table,))
        else:
            cur.execute(
                "INSERT OR REPLACE INTO table_name_map(src_table, dst_table) VALUES(?,?)",
                (src_table, dst_table)
            )


def set_table_name_map_bulk(mapping):
    """批量设置：(src, dst) 列表，dst 为空或等于 src 时删除"""
    with sqlite_cursor() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM table_name_map")
        for src, dst in mapping:
            if dst and dst != src:
                cur.execute(
                    "INSERT INTO table_name_map(src_table, dst_table) VALUES(?,?)",
                    (src, dst)
                )


# ============================================================
# 租户 ID 映射
# ============================================================

def list_tenant_map():
    """返回 [(src_tenantid, dst_tenantid)] 列表，按 src 排序"""
    with sqlite_cursor() as conn:
        rows = conn.cursor().execute(
            "SELECT src_tenantid, dst_tenantid FROM tenant_id_map ORDER BY src_tenantid"
        ).fetchall()
        return [(r["src_tenantid"], r["dst_tenantid"]) for r in rows]


def get_tenant_map_dict():
    """返回 {src: dst} 字典，方便迁移引擎构造 CASE WHEN"""
    with sqlite_cursor() as conn:
        rows = conn.cursor().execute(
            "SELECT src_tenantid, dst_tenantid FROM tenant_id_map"
        ).fetchall()
        return {r["src_tenantid"]: r["dst_tenantid"] for r in rows}


def upsert_tenant_map(src_tenantid, dst_tenantid):
    """设置单条映射；dst 为空时删除"""
    with sqlite_cursor() as conn:
        cur = conn.cursor()
        if not dst_tenantid:
            cur.execute("DELETE FROM tenant_id_map WHERE src_tenantid=?", (src_tenantid,))
        else:
            cur.execute(
                "INSERT INTO tenant_id_map(src_tenantid, dst_tenantid, updated_at) VALUES(?,?,datetime('now')) "
                "ON CONFLICT(src_tenantid) DO UPDATE SET dst_tenantid=excluded.dst_tenantid, "
                "updated_at=excluded.updated_at",
                (src_tenantid, dst_tenantid)
            )


def upsert_tenant_map_bulk(mapping):
    """mapping: {src: dst} 或 [(src, dst)] 列表；dst 为空时删除"""
    pairs = (mapping.items() if isinstance(mapping, dict) else mapping)
    with sqlite_cursor() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM tenant_id_map")
        for s, d in pairs:
            if d:
                cur.execute(
                    "INSERT INTO tenant_id_map(src_tenantid, dst_tenantid, updated_at) "
                    "VALUES(?,?,datetime('now'))",
                    (s, d)
                )


def scan_source_tenants(host, port, user, password, db, table, tenant_col="tenantid"):
    """扫描源表中所有出现过的 tenantid"""
    sql = (f"SELECT DISTINCT `{tenant_col}` AS tid, COUNT(*) AS cnt "
           f"FROM `{table}` WHERE `{tenant_col}` IS NOT NULL AND `{tenant_col}` != '' "
           f"GROUP BY `{tenant_col}` ORDER BY cnt DESC LIMIT 1000")
    conn = open_mysql(host=host, port=int(port), user=user, password=password, db=db)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return [{"tenantid": r["tid"], "count": r["cnt"]} for r in cur.fetchall()]
    finally:
        conn.close()


def get_field_mappings(table):
    """返回 (dst_field, src_field, default_value) 的 tuple 列表"""
    with sqlite_cursor() as conn:
        rows = conn.cursor().execute(
            "SELECT dst_field,src_field,default_value FROM field_mapping WHERE table_name=? ORDER BY rowid",
            (table,)
        ).fetchall()
        return [(r["dst_field"], r["src_field"], r["default_value"]) for r in rows]


def reset_task_progress(table):
    """重置指定表的迁移状态，保留字段映射与时间范围"""
    with sqlite_cursor() as conn:
        conn.cursor().execute(
            """UPDATE migrate_task SET status='pending',total=0,migrated=0,
            failed=0,last_pk=NULL,started_at=NULL,finished_at=NULL,err_msg=NULL
            WHERE table_name=?""", (table,)
        )


def reset_all():
    with sqlite_cursor() as conn:
        conn.cursor().execute("DELETE FROM migrate_log")
        conn.cursor().execute("DELETE FROM migrate_task")


# ============================================================
# MySQL
# ============================================================

def open_mysql(host, port, user, password, db, charset="utf8mb4"):
    return pymysql.connect(
        host=host, port=int(port), user=user, password=password,
        database=db, charset=charset, autocommit=False,
        cursorclass=DictCursor
    )


@contextmanager
def mysql_cursor(host, port, user, password, db):
    conn = open_mysql(host, port, user, password, db)
    try:
        cur = conn.cursor()
        yield cur, conn
    finally:
        conn.close()


def test_mysql(host, port, user, password, db):
    """测试 MySQL 连接"""
    try:
        conn = open_mysql(host, port, user, password, db, charset="utf8mb4")
        cur = conn.cursor()
        cur.execute("SELECT VERSION() v")
        ver = cur.fetchone()
        cur.execute("SELECT NOW() t")
        now = cur.fetchone()
        cur.execute("SELECT COUNT(*) c FROM information_schema.tables "
                    "WHERE table_schema=%s", (db,))
        tbl_count = cur.fetchone()
        conn.close()
        return True, {
            "version": ver["v"],
            "now": str(now["t"]),
            "table_count": tbl_count["c"],
        }
    except Exception as e:
        return False, {"error": str(e)}


def get_mysql_columns(host, port, user, password, db, table):
    """获取表的所有列定义（含默认值、是否可空、类型等）"""
    sql = """
    SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY,
           COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT, ORDINAL_POSITION
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
    ORDER BY ORDINAL_POSITION
    """
    with mysql_cursor(host, port, user, password, db) as (cur, _):
        cur.execute(sql, (db, table))
        return [dict(r) for r in cur.fetchall()]


def get_pk_column(host, port, user, password, db, table):
    """获取主键列名"""
    sql = """
    SELECT COLUMN_NAME FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_KEY='PRI'
    LIMIT 1
    """
    with mysql_cursor(host, port, user, password, db) as (cur, _):
        cur.execute(sql, (db, table))
        row = cur.fetchone()
        return row["COLUMN_NAME"] if row else "id"
