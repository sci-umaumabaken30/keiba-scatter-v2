import os
import subprocess

desktop = subprocess.check_output(
    ['powershell', '-Command', '[Environment]::GetFolderPath("Desktop")'],
    encoding='cp932'
).strip()

vbs = (
    'Set objShell = CreateObject("WScript.Shell")\r\n'
    'Set objHTTP = CreateObject("MSXML2.XMLHTTP")\r\n'
    'running = False\r\n'
    'On Error Resume Next\r\n'
    'objHTTP.Open "GET", "http://localhost:5001", False\r\n'
    'objHTTP.Send\r\n'
    'If objHTTP.Status = 200 Then running = True\r\n'
    'On Error GoTo 0\r\n'
    'If Not running Then\r\n'
    '    objShell.Run Chr(34) & "C:\\Users\\sena0\\AppData\\Local\\Programs\\Python\\Python312\\pythonw.exe" & Chr(34) & " " & Chr(34) & "C:\\Users\\sena0\\keiba-scatter-v2\\admin.py" & Chr(34), 0, False\r\n'
    '    WScript.Sleep 2000\r\n'
    'End If\r\n'
    'objShell.Run Chr(34) & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" & Chr(34) & " http://localhost:5001/", 1, False\r\n'
)

path = os.path.join(desktop, '\u7af6\u99ac\u6563\u5e03\u56f3.vbs')
with open(path, 'w', encoding='cp932') as f:
    f.write(vbs)
print('OK:', path)
