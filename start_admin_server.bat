@echo off
chcp 65001 >nul
echo ========================================
echo  keiba-scatter-v2 管理ダッシュボード
echo ========================================
echo.
echo  サーバーを起動中...

REM 既存プロセスを終了
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000 "') do (
    taskkill /PID %%a /F >nul 2>&1
)
ping -n 2 127.0.0.1 >nul

REM サーバー起動（コンソール表示あり）
start "keiba-admin" "C:\Users\sena0\AppData\Local\Programs\Python\Python312\python.exe" "C:\Users\sena0\keiba-scatter-v2\admin_server.py"
ping -n 4 127.0.0.1 >nul

start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" "http://localhost:5000/"
echo  http://localhost:5000/ を開きました
echo.
