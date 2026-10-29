@echo off
chcp 65001 >nul
cd /d "%~dp0."
python "Anime4K_Upscaler.py"
echo.
echo 程序已退出，按任意键关闭窗口。
pause >nul
