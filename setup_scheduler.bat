@echo off
setlocal

set PYTHON=C:\Users\sena0\AppData\Local\Programs\Python\Python312\pythonw.exe
set SCRIPT=C:\Users\sena0\keiba-scatter-v2\auto_update.py
set TASK=KeibaAutoUpdate

echo タスクスケジューラに登録中...
schtasks /delete /tn "%TASK%" /f 2>nul

:: 毎30分起動、auto_update.py 内でウィンドウ判定
:: 枠番: 金・土 11:00〜11:30
:: クッション値: 金曜 12:00〜14:30 / 土日 9:15〜9:45
schtasks /create /tn "%TASK%" ^
  /tr "\"%PYTHON%\" \"%SCRIPT%\"" ^
  /sc minute /mo 30 ^
  /st 09:00 /et 15:00 ^
  /f

echo.
echo 登録完了！
echo タスク名: %TASK%
echo 実行間隔: 毎30分 (9:00〜15:00)
echo ログ: C:\Users\sena0\keiba-scatter-v2\auto_update.log
echo.
pause
