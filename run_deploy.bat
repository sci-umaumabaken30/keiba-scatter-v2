@echo off
chcp 65001 >nul
echo ========================================
echo  GitHub Pages デプロイ
echo ========================================
echo.
set /p date=開催日を入力してください (YYYYMMDD):
python pipeline.py %date% --deploy
echo.
pause
