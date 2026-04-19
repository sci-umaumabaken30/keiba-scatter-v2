@echo off
chcp 65001 >nul
echo ========================================
echo  クッション値DB 更新
echo ========================================
echo.
python update_cushion_db.py
echo.
pause
