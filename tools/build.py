#!/usr/bin/env python3
"""
跨平台打包脚本：macOS / Windows / Linux

用法：
    python3 tools/build.py              # 安装 pyinstaller 并打包
    python3 tools/build.py --no-install # 跳过安装（已装好时）
    python3 tools/build.py --onedir     # 输出目录模式（启动更快）
"""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 默认 cp1252 编码无法打印中文 / Emoji，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
OS_NAME = platform.system().lower()
# PyInstaller 的 --add-data 分隔符：类 Unix 用 :，Windows 用 ;
DATA_SEP = ";" if OS_NAME == "windows" else ":"
EXE_NAME = "hrcloud-migrate.exe" if OS_NAME == "windows" else "hrcloud-migrate"


def clean():
    """清理上一次构建产物"""
    for d in ["build", "dist", "*.spec"]:
        for p in ROOT.glob(d):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def install_pyinstaller():
    print(">>> 安装 pyinstaller ...", flush=True)
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "pyinstaller>=6.0"]
    )


def build(onefile=True):
    fmt_flag = "--onefile" if onefile else "--onedir"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        fmt_flag,
        "--name=hrcloud-migrate",
        "--console",          # 保留控制台，方便查看 URL 与日志
        f"--add-data=templates{DATA_SEP}templates",
        f"--add-data=static{DATA_SEP}static",
        "--hidden-import=pymysql",
        "--hidden-import=flask",
        "--hidden-import=sqlite3",
        "--collect-submodules=pymysql",
        "--collect-submodules=flask",
        "--clean",
        "--noconfirm",
        "app.py",
    ]
    print(f">>> 在 {OS_NAME} 上打包 ({fmt_flag}) ...", flush=True)
    subprocess.check_call(cmd, cwd=ROOT)

    out = ROOT / "dist" / (EXE_NAME if onefile else "hrcloud-migrate")
    print(f"\n✅ 完成: {out}")
    if not onefile:
        # 在 onedir 模式下，windows exe 在 dist/hrcloud-migrate/ 目录内
        win_exe = ROOT / "dist" / "hrcloud-migrate" / EXE_NAME
        if win_exe.exists():
            print(f"   Windows 可执行: {win_exe}")
    return out


if __name__ == "__main__":
    no_install = "--no-install" in sys.argv
    onedir = "--onedir" in sys.argv
    if not no_install:
        try:
            install_pyinstaller()
        except subprocess.CalledProcessError as e:
            print(f"⚠️  pyinstaller 安装失败：{e}")
            print("   请手动执行: pip install pyinstaller")
            sys.exit(1)
    clean()
    build(onefile=not onedir)