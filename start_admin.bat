@echo off
chcp 65001 >nul
echo ========================================
echo  keiba-scatter-v2 管理ダッシュボード
echo ========================================
echo.
echo  サーバーを起動中...
start /b python "%~dp0admin.py"
ping -n 4 127.0.0.1 >nul
start "" http://127.0.0.1:5001/
echo  http://127.0.0.1:5001/ を開きました
echo.
echo  終了するには このウィンドウを閉じてください
echo.
python "%~dp0admin.py"
