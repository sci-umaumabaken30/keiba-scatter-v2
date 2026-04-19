#!/usr/bin/env python3
"""
GitHub Pages から __ダ0m / __芝0m / __障0m の壊れたファイルを削除し
インデックスを再生成するスクリプト
"""
import json, re, time, base64, os, requests
from urllib.parse import quote

DEPLOY_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'deploy_config.json')
CUSHION_DB_PATH    = os.path.join(os.path.dirname(__file__), 'cushion_db_full.json')

with open(DEPLOY_CONFIG_PATH, encoding='utf-8') as f:
    config = json.load(f)

token   = config['github_token']
repo    = config['repo']
headers = {
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github.v3+json',
}
api_base = f'https://api.github.com/repos/{repo}/contents'

# 全ファイル取得
r = requests.get(api_base, headers=headers)
r.raise_for_status()
all_files = {item['name']: item['sha'] for item in r.json()}

# 壊れたファイルパターン: __ダ0m.html / __芝0m.html / __障0m.html
BROKEN_RE = re.compile(r'scatter_.+__[ダ芝障]0m\.html$')

to_delete = {name: sha for name, sha in all_files.items() if BROKEN_RE.match(name)}
print(f"削除対象: {len(to_delete)}件")

for fname, sha in sorted(to_delete.items()):
    url = f'{api_base}/{quote(fname)}'
    payload = {'message': f'Cleanup broken: {fname}', 'sha': sha}
    r = requests.delete(url, headers=headers, json=payload)
    if r.status_code == 200:
        print(f"  Del {fname}")
    else:
        print(f"  NG  {fname}: {r.status_code}")
    time.sleep(0.8)

print("\n完了。インデックスを再生成します...")

# pipeline の _build_remote_index / deploy 関数を使ってindex.html を再生成
import sys
sys.path.insert(0, os.path.dirname(__file__))
from pipeline import _build_remote_index

r = requests.get(api_base, headers=headers)
remaining = {item['name']: item['sha'] for item in r.json()}

all_scatter = sorted(
    [f for f in remaining if f.startswith('scatter_') and f.endswith('.html')],
    reverse=True
)
date_groups = {}
for fname in all_scatter:
    m = re.match(r'scatter_(\d{8})_(.+)\.html', fname)
    if m:
        d = m.group(1)
        d_fmt = f"{d[:4]}/{d[4:6]}/{d[6:8]}"
        date_groups.setdefault(d_fmt, []).append(fname)

cushion_db = {}
if os.path.exists(CUSHION_DB_PATH):
    with open(CUSHION_DB_PATH, encoding='utf-8') as f:
        cushion_db = json.load(f)

index_html = _build_remote_index(date_groups, cushion_db)

idx_sha = remaining.get('index.html')
payload = {
    'message': 'Rebuild index after cleanup',
    'content': base64.b64encode(index_html.encode('utf-8')).decode(),
}
if idx_sha:
    payload['sha'] = idx_sha

r = requests.put(f'{api_base}/{quote("index.html")}', headers=headers, json=payload)
if r.status_code in (200, 201):
    print("index.html 更新完了")
else:
    print(f"index.html 更新失敗: {r.status_code}")
