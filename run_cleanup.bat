@echo off
chcp 65001 >nul
echo ========================================
echo  旧ファイル削除 + デプロイ
echo ========================================
echo.
set /p date=開催日を入力してください (YYYYMMDD):
python pipeline.py %date% --deploy --cleanup
echo.
pause
