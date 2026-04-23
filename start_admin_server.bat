@echo off
chcp 65001 >nul
echo ========================================
echo  keiba-scatter-v2 管理ダッシュボード
echo ========================================
echo.
echo  サーバーを起動中...
start /b "" "C:\Users\sena0\AppData\Local\Programs\Python\Python312\pythonw.exe" "C:\Users\sena0\keiba-scatter-v2\admin_server.py"
ping -n 3 127.0.0.1 >nul
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" "http://localhost:5000/"
echo  http://localhost:5000/ を開きました
echo.
pause
