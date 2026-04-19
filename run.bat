@echo off
chcp 65001 >nul
echo ========================================
echo  keiba-scatter-v2 実行メニュー
echo ========================================
echo.
echo  [1] 自動取得 + ローカル保存のみ
echo  [2] 自動取得 + GitHub Pagesへデプロイ
echo  [3] 手動入力 + ローカル保存のみ
echo  [4] 手動入力 + GitHub Pagesへデプロイ
echo.
set /p mode=モードを選択してください (1-4):

echo.
set /p date=開催日を入力してください (YYYYMMDD, 例: 20260215):

if "%mode%"=="1" (
    python pipeline.py %date%
) else if "%mode%"=="2" (
    python pipeline.py %date% --deploy
) else if "%mode%"=="3" (
    python pipeline.py %date% --manual
) else if "%mode%"=="4" (
    python pipeline.py %date% --manual --deploy
) else (
    echo 無効な選択です
)

echo.
pause
