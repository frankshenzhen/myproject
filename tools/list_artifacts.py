#!/usr/bin/env python3
"""列出指定目录的文件大小，给 CI 使用（避免 PowerShell / bash 引号差异）。"""
import os
import sys


def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024.0:
            return f"{num_bytes:6.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:6.1f} TB"


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "dist"
    if not os.path.isdir(target):
        print(f"(missing directory: {target})")
        return 1
    files = sorted(os.listdir(target))
    if not files:
        print("(empty)")
        return 0
    for name in files:
        path = os.path.join(target, name)
        if os.path.isfile(path):
            print(f"{human_size(os.path.getsize(path))}  {target}/{name}")
        else:
            print(f"{'    <dir>':>10}  {target}/{name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())