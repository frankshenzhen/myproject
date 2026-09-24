@echo off
REM 跨平台构建：Windows 批处理包装
REM 双击此文件即可一键打包
chcp 65001 > nul
cd /d "%~dp0\.."
echo.
echo ===============================================
echo   hrcloud-migrate Windows 打包
echo ===============================================
echo.
python tools\build.py %*
if errorlevel 1 (
    echo.
    echo [X] build failed
    pause
) else (
    echo.
    echo [V] build succeeded
    echo Output: dist\hrcloud-migrate.exe
    pause
)