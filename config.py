"""
数据迁移工具 - 全局配置
"""
import os
import sys
from pathlib import Path


def _is_frozen():
    """是否运行在 PyInstaller 打包后的可执行文件中"""
    return getattr(sys, "frozen", False)


def _resolve_base_dir():
    """应用数据根目录：
    - 开发模式：脚本所在目录
    - 打包后：可执行文件所在目录（用户可写、便于查找 data/logs）
    """
    if _is_frozen():
        return Path(sys.executable).parent.resolve()
    return Path(__file__).resolve().parent


BASE_DIR = _resolve_base_dir()

# 元数据存储
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
SQLITE_PATH = DATA_DIR / "migrate_meta.db"

# 首次启动时确保目录存在
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# Flask 配置
HOST = "0.0.0.0"
PORT = 8765

# 需要迁移的表（用户指定）
TABLES = [
    "ts_leave_apply",
    "ts_leave_apply_detail",
    "ts_leave_off_detail",
    "ts_business_trip_apply",
    "ts_business_trip_apply_detail",
    "ts_business_trip_revoke_detail",
]

# 字段配置：源库时间字段（区分大小写，按 information_schema 实际命名）
TABLE_TIME_FIELDS = {
    "ts_leave_apply":              "leavebegintime",
    "ts_leave_apply_detail":       "LEAVEBEGINTIME",
    "ts_leave_off_detail":         "LEAVEOFFBEGINTIME",
    "ts_business_trip_apply":      "tripbegintime",
    "ts_business_trip_apply_detail": "tripbegintime",
    "ts_business_trip_revoke_detail": "tripbegintime",
}

# 每张表主键字段（默认 id）
TABLE_PRIMARY_KEYS = {
    "ts_leave_apply":              "id",
    "ts_leave_apply_detail":       "id",
    "ts_leave_off_detail":         "id",
    "ts_business_trip_apply":      "id",
    "ts_business_trip_apply_detail": "id",
    "ts_business_trip_revoke_detail": "id",
}

# 字段类型映射：解决源-目标库类型不一致（比如字符串长度变化、int 长度变化）
# 不在本表内的字段视为完全兼容
TYPE_CAST_OVERRIDE = {
    # decimal 精度问题，需要数据迁移时 CAST 处理
}

# 默认迁移批次大小
DEFAULT_BATCH_SIZE = 500

# 子批大小：一次 INSERT executemany 的行数上限，避免单语句过大或事务过长
SUB_BATCH_SIZE = 200

# 租户 ID 字段（不论大小写）：源 tenantid 和目标 ytenant_id 都套 CASE WHEN 映射
TENANT_FIELDS = {"tenantid", "ytenant_id"}

# 仅目标库有、需要提供默认值的字段（按表分组）
# 字段名为目标库字段名
DEFAULT_VALUES = {
    "ts_leave_apply": {
        # code: 单据编号 -> 同步自 id 或保留空
        "code": "",
        "status": "1",
        "vouchdate": None,  # 留空由数据库默认（一般为空）
        "bd_staffid": "",
        "account_org_id": "",
        "inner_leave_type_id": "",
        "breastfeedLeaveDay": "0",
    },
    "ts_leave_off_detail": {
        "code": "",
        "status": "1",
        "vouchdate": None,
        "filepath": "",
        "inner_leave_type_id": "",
        "leavetypeid": "",
        "orgid": "",
        "last_flag": "0",
        "account_org_id": "",
        "breastfeedLeaveOffDay": "0",
        "old_code": "",
        "old_filepath": "",
        "old_remark": "",
        "old_showbegindate": None,
        "old_showenddate": None,
    },
    "ts_business_trip_apply": {
        "code": "",
        "vouchdate": None,
        "deptvid": "",
        "orgvid": "",
        "staffjobid": "",
        "system_code": "",
        "account_org_id": "",
        "end_day_type": "0",
        "start_day_type": "0",
        "whole_day": "0",
        "showbegindate": None,
        "showenddate": None,
        "newstaffid": "",
        "innertriptypeid": "",
    },
    "ts_business_trip_apply_detail": {
        "end_day_type": "0",
        "start_day_type": "0",
        "whole_day": "0",
        "showbegindate": None,
        "showenddate": None,
    },
    "ts_business_trip_revoke_detail": {
        "code": "",
        "vouchdate": None,
        "applicant": "",
        "orgid": "",
        "end_day_type": "0",
        "start_day_type": "0",
        "last_flag": "0",
        "whole_day": "0",
        "showbegindate": None,
        "showenddate": None,
        "old_end_day_type": "0",
        "old_start_day_type": "0",
        "oldshowbegindate": None,
        "oldshowenddate": None,
        "newstaffid": "",
        "innertriptypeid": "",
    },
    # ts_leave_apply_detail 暂无目标独有字段
}
