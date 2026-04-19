@echo off
chcp 65001 >nul
echo ========================================
echo  keiba-scatter-v2 セットアップ
echo ========================================
echo.
echo Pythonパッケージをインストールします...
pip install -r requirements.txt
echo.
echo セットアップ完了！
echo deploy_config.json にGitHubトークンとリポジトリ名を設定してください。
echo.
pause
