"""
请假出差数据迁移工具 - Web入口
Flask + SQLite + MySQL
"""
import json
import logging
import os
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from config import (
    DATA_DIR, DEFAULT_BATCH_SIZE, HOST, LOG_DIR, PORT, TABLES, TABLE_TIME_FIELDS,
)
from db import (
    get_all_configs, init_sqlite, list_tasks, recent_logs, reset_all,
    reset_task_progress, set_config, set_table_name_map_bulk,
    get_dst_table, get_table_name_map,
    list_tenant_map, get_tenant_map_dict, upsert_tenant_map_bulk,
    scan_source_tenants,
    sqlite_cursor, test_mysql, upsert_task, get_task,
)
from migrate_engine import (
    build_field_mapping, compute_and_store_mapping, get_state,
    migrate_table, request_cancel, start_migration,
)


def _resource_path(rel):
    """资源绝对路径（兼容 PyInstaller onefile 模式）
    PyInstaller 会在 _MEIPASS 临时目录解压资源；开发模式则使用脚本目录。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).parent
    return base / rel


# ==================== 初始化 ====================
init_sqlite()
app = Flask(
    __name__,
    template_folder=str(_resource_path("templates")),
    static_folder=str(_resource_path("static")),
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "migrate.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("app")


# ==================== 页面路由 ====================

@app.route("/")
def index():
    return render_template("index.html", tables=TABLES, time_fields=TABLE_TIME_FIELDS)


@app.route("/config")
def config_page():
    cfg = get_all_configs()
    return render_template("config.html", cfg=cfg)


@app.route("/tables")
def tables_page():
    tasks = list_tasks()
    cfg = get_all_configs()
    has_db = bool(cfg.get("src_host") and cfg.get("dst_host"))
    return render_template("tables.html",
                           tables=TABLES,
                           time_fields=TABLE_TIME_FIELDS,
                           tasks=tasks,
                           has_db=has_db,
                           cfg=cfg)


@app.route("/progress")
def progress_page():
    return render_template("progress.html", tables=TABLES)


@app.route("/logs")
def logs_page():
    table = request.args.get("table")
    logs = recent_logs(table=table, limit=200)
    return render_template("logs.html", logs=logs, table=table, tables=TABLES)


# ==================== API ====================

@app.route("/api/config", methods=["POST"])
def api_save_config():
    """保存数据库连接配置"""
    payload = request.get_json() or {}
    for k, v in payload.items():
        if v is None:
            continue
        set_config(k, str(v))
    return jsonify({"code": 0, "msg": "配置已保存"})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(get_all_configs())


@app.route("/api/test", methods=["POST"])
def api_test_connection():
    payload = request.get_json() or {}
    role = payload.get("role", "src")  # src / dst
    cfg = get_all_configs()
    # 合并：前端 payload 覆盖 db_config 已存；如果前端为空就 fallback 到已存配置
    def _get(k):
        return payload.get(k) or cfg.get(f"{role}_{k}") or cfg.get(k)
    host = _get("host")
    port = int(_get("port") or 3306)
    user = _get("user")
    password = _get("password") or cfg.get(f"{role}_password") or ""
    db = _get("db")
    if not (host and user and db):
        return jsonify({"code": 1,
                        "data": {"error": f"缺少连接信息：host={host}, user={user}, db={db}"},
                        "msg": "请先在「数据库配置」填写并保存"})
    ok, info = test_mysql(host, port, user, password, db)
    if ok:
        return jsonify({"code": 0, "data": info, "msg": "连接成功"})
    return jsonify({"code": 1, "data": info, "msg": "连接失败"})


@app.route("/api/mapping/<table>", methods=["POST"])
def api_build_mapping(table):
    """生成字段映射"""
    if table not in TABLES:
        return jsonify({"code": 1, "msg": "非法表名"})
    cfg = get_all_configs()
    miss = _missing_cfg_fields(cfg)
    if miss:
        return jsonify({"code": 1, "msg": f"请先在「数据库配置」填写: {', '.join(miss)}"})
    try:
        # 读取目标表名映射，必须显式配置且与源表不同名
        dst_table = get_dst_table(table)
        if not dst_table:
            return jsonify({"code": 1,
                            "msg": f"表 {table} 目标表未配置，请先在「表管理」配置源-目标表名映射"})
        if dst_table == table:
            return jsonify({"code": 1,
                            "msg": f"表 {table} 目标表与源表同名，禁止迁移"})
        src_cfg = _select_db_cfg(cfg, "src")
        dst_cfg = _select_db_cfg(cfg, "dst")
        mappings, notes = compute_and_store_mapping(
            src_cfg["host"], src_cfg["port"], src_cfg["user"], src_cfg["password"], src_cfg["db"],
            dst_cfg["host"], dst_cfg["port"], dst_cfg["user"], dst_cfg["password"], dst_cfg["db"],
            table, dst_table=dst_table,
        )
        return jsonify({"code": 0, "data": {
            "dst_table": dst_table,
            "mappings": [
                {"dst": d, "src": s, "default": v}
                for (d, s, v) in mappings
            ],
            "notes": notes,
        }})
    except Exception as e:
        return jsonify({"code": 1, "msg": str(e)})


def _select_db_cfg(cfg, role):
    """从 cfg 中读取 src/dst 连接信息，role ∈ {src, dst}"""
    return {
        "host": cfg.get(f"{role}_host") or cfg.get("host"),
        "port": int(cfg.get(f"{role}_port") or 3306),
        "user": cfg.get(f"{role}_user") or cfg.get("user"),
        "password": cfg.get(f"{role}_password") or cfg.get("password"),
        "db": cfg.get(f"{role}_db") or cfg.get("db"),
    }


def _missing_cfg_fields(cfg):
    """检查 cfg 中是否有缺失的关键连接字段"""
    needed = ["src_host", "src_port", "src_user", "src_password", "src_db",
              "dst_host", "dst_port", "dst_user", "dst_password", "dst_db"]
    return [k for k in needed if not cfg.get(k)]


@app.route("/api/table_map", methods=["GET"])
def api_get_table_map():
    """获取源-目标表名映射"""
    from db import get_table_name_map
    return jsonify({"code": 0, "data": get_table_name_map()})


@app.route("/api/table_map", methods=["POST"])
def api_set_table_map():
    """批量设置表名映射：{src_table: dst_table}，dst 为空表示删除映射"""
    payload = request.get_json() or {}
    mapping = payload.get("mapping") or {}
    pairs = [(k, v) for k, v in mapping.items() if k in TABLES]
    set_table_name_map_bulk(pairs)
    return jsonify({"code": 0, "msg": "已保存", "data": get_table_name_map()})


# ============================================================
# 租户 ID 映射
# ============================================================

@app.route("/api/tenant_map", methods=["GET"])
def api_get_tenant_map():
    return jsonify({"code": 0, "data": list_tenant_map()})


@app.route("/api/tenant_map", methods=["POST"])
def api_set_tenant_map():
    """mapping: {src_tenantid: dst_tenantid}；dst 为空字符串表示删除该条"""
    payload = request.get_json() or {}
    mapping = payload.get("mapping") or {}
    upsert_tenant_map_bulk(mapping)
    return jsonify({"code": 0, "msg": "已保存", "data": list_tenant_map()})


@app.route("/api/tenant_map/scan/<table>", methods=["POST"])
def api_scan_tenant(table):
    """扫描源表中已存在的 tenantid（用于帮助用户配置映射）"""
    if table not in TABLES:
        return jsonify({"code": 1, "msg": "非法表名"})
    cfg = get_all_configs()
    miss = _missing_cfg_fields(cfg)
    if miss:
        return jsonify({"code": 1, "msg": f"请先在「数据库配置」填写: {', '.join(miss)}"})
    payload = request.get_json() or {}
    tenant_col = payload.get("tenant_col") or "tenantid"
    try:
        rows = scan_source_tenants(
            cfg["src_host"], cfg["src_port"], cfg["src_user"], cfg["src_password"], cfg["src_db"],
            table, tenant_col=tenant_col)
        # 同时附上当前已映射的 dst（用于回显）
        cur_map = {s: d for s, d in list_tenant_map()}
        return jsonify({"code": 0, "data": {"tenants": rows, "existing_map": cur_map}})
    except Exception as e:
        return jsonify({"code": 1, "msg": str(e)})


@app.route("/api/mapping/<table>", methods=["GET"])
def api_get_mapping(table):
    if table not in TABLES:
        return jsonify({"code": 1, "msg": "非法表名"})
    from db import get_field_mappings
    mappings = get_field_mappings(table)
    return jsonify({"code": 0, "data": [dict(m) for m in mappings]})


@app.route("/api/mapping", methods=["POST"])
def api_save_mapping():
    """修改字段映射或默认值"""
    payload = request.get_json() or {}
    table = payload.get("table")
    mappings = payload.get("mappings") or []
    if table not in TABLES:
        return jsonify({"code": 1, "msg": "非法表名"})
    with sqlite_cursor() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM field_mapping WHERE table_name=?", (table,))
        for m in mappings:
            cur.execute(
                "INSERT INTO field_mapping(table_name,dst_field,src_field,default_value) "
                "VALUES(?,?,?,?)",
                (table, m.get("dst"), m.get("src") or None,
                 "" if m.get("default") is None else str(m.get("default")))
            )
    return jsonify({"code": 0, "msg": "字段映射已保存"})


@app.route("/api/migrate", methods=["POST"])
def api_start_migrate():
    payload = request.get_json() or {}
    table = payload.get("table")
    if table not in TABLES:
        return jsonify({"code": 1, "msg": "非法表名"})
    # 预校验目标表
    dst_table = get_dst_table(table)
    if not dst_table:
        return jsonify({"code": 1,
                        "msg": f"表 {table} 目标表未配置，请先在「表管理」配置源-目标表名映射"})
    if dst_table == table:
        return jsonify({"code": 1,
                        "msg": f"表 {table} 目标表与源表同名，禁止迁移"})
    page_size = int(payload.get("page_size") or DEFAULT_BATCH_SIZE)
    time_from = payload.get("time_from") or ""
    time_to = payload.get("time_to") or ""
    # 预统计：若所有源记录已迁则不启动后台线程，直接提示
    try:
        cnt = migrate_table(table, time_from=time_from, time_to=time_to, only_count=True)
        if cnt.get("total", 0) == 0:
            return jsonify({
                "code": 0,
                "msg": "无需迁移：所有源记录均已存在于目标表",
                "data": {**cnt, "no_need": True},
            })
    except Exception as e:
        return jsonify({"code": 1, "msg": str(e)})
    ok, msg = start_migration(table, page_size, time_from, time_to)
    return jsonify({"code": 0 if ok else 1, "msg": msg})


@app.route("/api/migrate/stop", methods=["POST"])
def api_stop_migrate():
    """通过进程内标志位实现软停止（当前批次完成后不再继续）"""
    payload = request.get_json() or {}
    table = payload.get("table")
    if not table:
        return jsonify({"code": 1, "msg": "缺少 table"})
    running = request_cancel()
    return jsonify({"code": 0,
                    "msg": (f"{table} 已标记为暂停（运行中={running}），"
                            "将在当前批次完成后停止（断点已保存）" if running
                            else f"{table} 当前无运行中的任务")})


@app.route("/api/progress", methods=["GET"])
def api_progress():
    """获取所有任务进度 + 当前状态"""
    tasks = list_tasks()
    state = get_state()
    summary = {
        "total": sum((t.get("total") or 0) for t in tasks),
        "migrated": sum((t.get("migrated") or 0) for t in tasks),
        "failed": sum((t.get("failed") or 0) for t in tasks),
        "running": state.get("running", False),
        "current_table": state.get("current_table"),
    }
    return jsonify({"code": 0, "data": {"summary": summary, "tasks": tasks, "state": state}})


@app.route("/api/progress/<table>", methods=["POST"])
def api_force_count(table):
    """重新统计行数"""
    if table not in TABLES:
        return jsonify({"code": 1, "msg": "非法表名"})
    payload = request.get_json() or {}
    time_from = payload.get("time_from") or ""
    time_to = payload.get("time_to") or ""
    try:
        result = migrate_table(table, time_from=time_from, time_to=time_to, only_count=True)
        return jsonify({"code": 0, "data": result})
    except Exception as e:
        return jsonify({"code": 1, "msg": str(e)})


@app.route("/api/reset/<table>", methods=["POST"])
def api_reset(table):
    if table not in TABLES:
        return jsonify({"code": 1, "msg": "非法表名"})
    reset_task_progress(table)
    return jsonify({"code": 0, "msg": f"已重置 {table} 进度"})


@app.route("/api/reset_all", methods=["POST"])
def api_reset_all():
    reset_all()
    return jsonify({"code": 0, "msg": "已重置所有进度与日志"})


@app.route("/api/logs", methods=["GET"])
def api_logs():
    table = request.args.get("table")
    limit = int(request.args.get("limit", 50))
    return jsonify({"code": 0, "data": recent_logs(table=table, limit=limit)})


# ==================== 错误处理 ====================

@app.errorhandler(Exception)
def handle_error(e):
    log.exception("未处理异常: %s", e)
    return jsonify({"code": 500, "msg": str(e)}), 500


if __name__ == "__main__":
    print(f"\n==== 请假出差数据迁移工具 ====")
    print(f"启动:    http://localhost:{PORT}/")
    print(f"数据目录: {DATA_DIR}")
    print(f"日志文件: {LOG_DIR}/migrate.log\n")
    # 打包后自动打开浏览器（开发模式由用户手动访问）
    if getattr(sys, "frozen", False):
        threading.Timer(
            1.5, lambda: webbrowser.open(f"http://localhost:{PORT}/")
        ).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
