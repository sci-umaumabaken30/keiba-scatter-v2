import os
import subprocess

desktop = subprocess.check_output(
    ['powershell', '-Command', '[Environment]::GetFolderPath("Desktop")'],
    encoding='cp932'
).strip()

bat_src = 'C:\\Users\\sena0\\keiba-scatter-v2\\start_admin_server.bat'

# \u30c7\u30b9\u30af\u30c8\u30c3\u30d7\u306b.lnk\u30b7\u30e7\u30fc\u30c8\u30ab\u30c3\u30c8\u3092\u4f5c\u6210
lnk_path = os.path.join(desktop, '\u7af6\u99ac\u6563\u5e03\u56f3.lnk')
ps_cmd = (
    f'$ws = New-Object -ComObject WScript.Shell; '
    f'$sc = $ws.CreateShortcut("{lnk_path}"); '
    f'$sc.TargetPath = "{bat_src}"; '
    f'$sc.WorkingDirectory = "C:\\Users\\sena0\\keiba-scatter-v2"; '
    f'$sc.Save()'
)
subprocess.run(['powershell', '-Command', ps_cmd], check=True)
print('OK:', lnk_path)
