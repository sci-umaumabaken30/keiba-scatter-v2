#!/usr/bin/env python3
"""
競馬散布図 管理サーバー
起動: python admin_server.py
ブラウザ: http://localhost:5000
"""

import subprocess
import sys
import json
import os
import logging
from datetime import datetime, timedelta
from flask import Flask, Response, request, render_template_string

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# バックグラウンド起動時のためファイルにログ出力
log_path = os.path.join(BASE_DIR, 'admin_server.log')
logging.basicConfig(
    filename=log_path,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logging.getLogger('werkzeug').setLevel(logging.WARNING)
app = Flask(__name__)

ADMIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>管理画面 | クッション値×含水率 散布図</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
body { background: linear-gradient(160deg,#1e3a72 0%,#162d58 50%,#1a3268 100%); color: #ddeeff; font-family: -apple-system, sans-serif; min-height: 100vh; }
.header {
  background: linear-gradient(180deg,rgba(255,255,255,0.10) 0%,rgba(255,255,255,0.02) 100%),#2d4a68;
  border-bottom: 1px solid rgba(255,255,255,0.12);
  padding: 16px 24px; display: flex; align-items: center; gap: 16px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.22), 0 4px 20px rgba(0,0,0,0.4);
  position: sticky; top: 0; z-index: 100;
}
.header h1 { font-size: 18px; font-weight: 800; color: #fff; }
.header .badge {
  background: linear-gradient(180deg,rgba(255,255,255,0.2) 0%,rgba(255,255,255,0.05) 100%),#f59e0b;
  color: #fff; font-size: 11px; font-weight: 700;
  padding: 3px 10px; border-radius: 6px;
  border: 1px solid rgba(255,255,255,0.3); border-top: 1px solid rgba(255,255,255,0.5);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.3), 0 2px 6px rgba(0,0,0,0.3);
}
.container { max-width: 800px; margin: 32px auto; padding: 0 16px; }

.card {
  background: linear-gradient(180deg,rgba(255,255,255,0.07) 0%,rgba(255,255,255,0.01) 100%),#2d4a68;
  border: 1px solid rgba(255,255,255,0.14); border-top: 1px solid rgba(255,255,255,0.25);
  border-radius: 14px; padding: 24px; margin-bottom: 20px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.15), 0 6px 24px rgba(0,0,0,0.35);
}
.card h2 { font-size: 13px; font-weight: 700; color: #a8c8e8; margin-bottom: 16px; letter-spacing: 0.08em; text-transform: uppercase; }

.form-row { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 16px; }
.form-group { display: flex; flex-direction: column; gap: 6px; }
.form-group label { font-size: 12px; color: #a8c8e8; font-weight: 600; }
.form-group input[type="date"], .form-group select {
  background: linear-gradient(180deg,rgba(255,255,255,0.08) 0%,rgba(255,255,255,0.02) 100%),#1a5276;
  border: 1px solid rgba(255,255,255,0.18); border-top: 1px solid rgba(255,255,255,0.3);
  border-radius: 8px; color: #ddeeff; padding: 8px 12px; font-size: 14px; outline: none;
  transition: border-color 0.15s; box-shadow: inset 0 1px 0 rgba(255,255,255,0.12);
}
.form-group input[type="date"]:focus, .form-group select:focus {
  border-color: rgba(245,158,11,0.8);
}

.checks { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
.check-label {
  display: flex; align-items: center; gap: 8px; cursor: pointer;
  font-size: 13px; color: #c8e0f8; user-select: none;
}
.check-label input[type="checkbox"] { accent-color: #f59e0b; width: 16px; height: 16px; }

.btn-run {
  background: linear-gradient(180deg,rgba(255,255,255,0.18) 0%,rgba(255,255,255,0.05) 100%),#f59e0b;
  color: #fff; font-weight: 800; font-size: 15px;
  border: 1px solid rgba(255,255,255,0.3); border-top: 1px solid rgba(255,255,255,0.5);
  border-radius: 10px; padding: 12px 28px; cursor: pointer;
  transition: all 0.15s; white-space: nowrap;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.3), 0 4px 14px rgba(0,0,0,0.35);
}
.btn-run:hover { background: linear-gradient(180deg,rgba(255,255,255,0.25) 0%,rgba(255,255,255,0.08) 100%),#f59e0b; }
.btn-run:active { transform: scale(0.97); }
.btn-run:disabled { background: linear-gradient(180deg,rgba(255,255,255,0.05) 0%,rgba(0,0,0,0.05) 100%),#2d4a68; color: #6a90b8; cursor: not-allowed; transform: none; box-shadow: none; border-color: rgba(255,255,255,0.1); }

.btn-open {
  background: linear-gradient(180deg,rgba(255,255,255,0.10) 0%,rgba(255,255,255,0.02) 100%),#1a5276;
  border: 1px solid rgba(255,255,255,0.18); border-top: 1px solid rgba(255,255,255,0.3);
  color: #c8e0f8; font-size: 13px; border-radius: 8px; padding: 8px 16px; cursor: pointer;
  transition: all 0.15s; text-decoration: none; display: inline-block;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.15), 0 2px 8px rgba(0,0,0,0.25);
}
.btn-open:hover { border-color: rgba(245,158,11,0.7); color: #f59e0b; }

.log-area {
  background: linear-gradient(180deg,rgba(0,0,0,0.2) 0%,rgba(0,0,0,0.1) 100%),#162d58;
  border: 1px solid rgba(255,255,255,0.1); border-radius: 10px;
  padding: 16px; font-family: 'Menlo', 'Consolas', monospace; font-size: 12px;
  line-height: 1.7; height: 400px; overflow-y: auto; white-space: pre-wrap;
  color: #a8c8e8; box-shadow: inset 0 2px 8px rgba(0,0,0,0.3);
}
.log-area .ok { color: #34d399; }
.log-area .err { color: #f87171; }
.log-area .head { color: #f59e0b; font-weight: 700; }
.log-area .info { color: #60a5fa; }

.status-bar {
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
  font-size: 12px; color: #7aa8c8;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: #3a6d9a; }
.dot.running { background: #f59e0b; animation: pulse 1s infinite; box-shadow: 0 0 8px #f59e0b; }
.dot.done { background: #34d399; box-shadow: 0 0 8px #34d399; }
.dot.error { background: #f87171; box-shadow: 0 0 8px #f87171; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

.dates-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.date-chip {
  background: linear-gradient(180deg,rgba(255,255,255,0.08) 0%,rgba(255,255,255,0.01) 100%),#1a5276;
  border: 1px solid rgba(255,255,255,0.15); border-top: 1px solid rgba(255,255,255,0.25);
  border-radius: 8px; padding: 6px 14px; font-size: 12px; color: #c8e0f8; cursor: pointer;
  transition: all 0.15s; box-shadow: inset 0 1px 0 rgba(255,255,255,0.12);
}
.date-chip:hover { border-color: rgba(245,158,11,0.7); color: #f59e0b; }

a.site-link { color: #f59e0b; text-decoration: none; font-size: 13px; font-weight: 700; }
a.site-link:hover { text-decoration: underline; }
</style>
</head>
<body>

<div class="header">
  <h1>クッション値×含水率 散布図</h1>
  <span class="badge">管理画面</span>
  <a class="site-link" href="https://jm3hiromu30-bit.github.io/keiba-scatter-v2/" target="_blank" style="margin-left:auto">
    ▶ サイトを開く
  </a>
</div>

<div class="container">

  <!-- 実行パネル -->
  <div class="card">
    <h2>スクレイピング実行</h2>
    <div class="form-row">
      <div class="form-group">
        <label>日付</label>
        <input type="date" id="date-input" value="">
      </div>
      <div class="form-group">
        <label>会場（任意）</label>
        <select id="venue-input">
          <option value="">全会場</option>
          <option>中山</option><option>東京</option><option>阪神</option>
          <option>京都</option><option>中京</option><option>小倉</option>
          <option>福島</option><option>新潟</option><option>札幌</option>
          <option>函館</option>
        </select>
      </div>
      <div class="form-group" style="justify-content:flex-end">
        <div class="checks">
          <label class="check-label">
            <input type="checkbox" id="chk-deploy" checked>
            GitHubへデプロイ
          </label>
          <label class="check-label">
            <input type="checkbox" id="chk-no-scrape">
            再スクレイピングなし
          </label>
        </div>
      </div>
    </div>
    <div style="display:flex;gap:12px;align-items:center">
      <button class="btn-run" id="btn-run" onclick="runPipeline()">▶ 実行</button>
      <button class="btn-open" onclick="stopPipeline()">■ 停止</button>
    </div>
  </div>

  <!-- ログ -->
  <div class="card">
    <h2>実行ログ</h2>
    <div class="status-bar">
      <div class="dot" id="status-dot"></div>
      <span id="status-text">待機中</span>
    </div>
    <div class="log-area" id="log"></div>
  </div>

  <!-- 過去データ一括取得 -->
  <div class="card">
    <h2>過去データ一括取得（AI学習用）</h2>
    <p style="font-size:12px;color:#7aa8c8;margin-bottom:14px">日付範囲を指定して過去レースデータを一括スクレイピングします。馬の過去成績キャッシュが蓄積されAI予測の精度向上に使えます。</p>
    <div class="form-row">
      <div class="form-group">
        <label>取得開始日</label>
        <input type="date" id="batch-from">
      </div>
      <div class="form-group">
        <label>取得終了日</label>
        <input type="date" id="batch-to">
      </div>
    </div>
    <div class="checks" style="margin-bottom:14px">
      <label class="check-label">
        <input type="checkbox" id="chk-batch-weekend" checked> 土日のみ（JRA開催日）
      </label>
      <label class="check-label">
        <input type="checkbox" id="chk-batch-deploy"> GitHubへデプロイ
      </label>
    </div>
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <button class="btn-run" id="btn-batch" style="background:linear-gradient(180deg,rgba(255,255,255,0.18) 0%,rgba(255,255,255,0.05) 100%),#7c3aed" onclick="runBatch()">▶ 一括取得開始</button>
      <button class="btn-open" onclick="stopPipeline()">■ 停止</button>
      <span id="batch-progress" style="font-size:12px;color:#a8c8e8"></span>
    </div>
  </div>

  <!-- クッション値DB更新 -->
  <div class="card">
    <h2>クッション値DB更新</h2>
    <p style="font-size:12px;color:#7aa8c8;margin-bottom:10px">週末前にJRA公式からクッション値・含水率を取得してDBを更新します</p>
    <p style="font-size:12px;margin-bottom:14px">期間: <span id="db-range" style="color:#f59e0b;font-weight:700">読込中...</span> &nbsp;<span id="db-count" style="color:#7aa8c8"></span></p>
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <button class="btn-run" id="btn-db" style="background:linear-gradient(180deg,rgba(255,255,255,0.18) 0%,rgba(255,255,255,0.05) 100%),#3b82f6" onclick="runUpdateDB()">↻ DB更新</button>
      <button class="btn-run" id="btn-weekend" style="background:linear-gradient(180deg,rgba(255,255,255,0.18) 0%,rgba(255,255,255,0.05) 100%),#10b981" onclick="runWeekendUpdate()">🔄 今週末を一括更新</button>
      <label class="check-label">
        <input type="checkbox" id="chk-year"> 過去データも取得（時間がかかります）
      </label>
    </div>
    <p style="font-size:11px;color:#5a80a8;margin-top:10px">一括更新: DB更新 → 今週土日のパイプライン自動実行（再スクレイピングなし）</p>
  </div>

</div>

<script>
let evtSource = null;

// 今日の日付をデフォルトに
const today = new Date();
const pad = n => String(n).padStart(2,'0');
document.getElementById('date-input').value =
  `${today.getFullYear()}-${pad(today.getMonth()+1)}-${pad(today.getDate())}`;

// 一括取得: デフォルト3ヶ月前〜今日
const threeMonthsAgo = new Date(today); threeMonthsAgo.setMonth(threeMonthsAgo.getMonth()-3);
document.getElementById('batch-from').value =
  `${threeMonthsAgo.getFullYear()}-${pad(threeMonthsAgo.getMonth()+1)}-${pad(threeMonthsAgo.getDate())}`;
document.getElementById('batch-to').value =
  `${today.getFullYear()}-${pad(today.getMonth()+1)}-${pad(today.getDate())}`;



function setStatus(state, text){
  const dot = document.getElementById('status-dot');
  dot.className='dot '+(state||'');
  document.getElementById('status-text').textContent=text;
}

function appendLog(text, cls){
  const log = document.getElementById('log');
  const span = document.createElement('span');
  if(cls) span.className=cls;
  span.textContent=text+'\\n';
  log.appendChild(span);
  log.scrollTop=log.scrollHeight;
}

function runPipeline(){
  if(evtSource){ evtSource.close(); evtSource=null; }

  const dateVal = document.getElementById('date-input').value.replace(/-/g,'');
  if(!dateVal){ alert('日付を選択してください'); return; }
  const deploy = document.getElementById('chk-deploy').checked;
  const noScrape = document.getElementById('chk-no-scrape').checked;
  const venue = document.getElementById('venue-input').value;

  document.getElementById('log').innerHTML='';
  document.getElementById('btn-run').disabled=true;
  setStatus('running','実行中...');

  const params = new URLSearchParams({date:dateVal, deploy, no_scrape:noScrape, venue});
  evtSource = new EventSource('/api/run?'+params);

  evtSource.onmessage = e => {
    const line = JSON.parse(e.data);
    let cls='';
    if(line.startsWith('  ✓')) cls='ok';
    else if(line.startsWith('  ✗') || line.includes('Error') || line.includes('エラー')) cls='err';
    else if(line.startsWith('===') || line.startsWith('[Step') || line.startsWith('[Deploy]')) cls='head';
    else if(line.startsWith('  ') && (line.includes('CV=') || line.includes('件'))) cls='info';

    if(line==='__DONE__'){
      setStatus('done','完了');
      document.getElementById('btn-run').disabled=false;
      evtSource.close(); evtSource=null;
    } else if(line==='__ERROR__'){
      setStatus('error','エラー');
      document.getElementById('btn-run').disabled=false;
      evtSource.close(); evtSource=null;
    } else {
      appendLog(line, cls);
    }
  };
  evtSource.onerror = ()=>{
    setStatus('error','接続エラー');
    document.getElementById('btn-run').disabled=false;
    evtSource.close(); evtSource=null;
  };
}

function stopPipeline(){
  if(evtSource){ evtSource.close(); evtSource=null; }
  fetch('/api/stop');
  setStatus('','停止');
  document.getElementById('btn-run').disabled=false;
  const bb=document.getElementById('btn-batch');
  if(bb){ bb.disabled=false; bb.textContent='▶ 一括取得開始'; }
}

function runUpdateDB(){
  if(evtSource){ evtSource.close(); evtSource=null; }
  document.getElementById('log').innerHTML='';
  setStatus('running','DB更新中...');
  const btn = document.getElementById('btn-db');
  btn.disabled = true;
  btn.textContent = '⏳ 更新中...';
  const withYear = document.getElementById('chk-year').checked;
  evtSource = new EventSource('/api/update_db?with_year=' + withYear);
  evtSource.onmessage = e => {
    const line = JSON.parse(e.data);
    let cls='';
    if(line.includes('追加') || line.includes('完了')) cls='ok';
    else if(line.includes('ERROR') || line.includes('エラー')) cls='err';
    else if(line.startsWith('===')) cls='head';
    if(line==='__DONE__'){
      setStatus('done','DB更新完了');
      btn.disabled=false; btn.textContent='↻ DB更新';
      evtSource.close(); evtSource=null;
    } else if(line==='__ERROR__'){
      setStatus('error','エラー');
      btn.disabled=false; btn.textContent='↻ DB更新';
      evtSource.close(); evtSource=null;
    } else { appendLog(line, cls); }
  };
  evtSource.onerror = ()=>{
    setStatus('error','接続エラー');
    btn.disabled=false; btn.textContent='↻ DB更新';
    evtSource.close(); evtSource=null;
  };
}

function runBatch(){
  const fromVal = document.getElementById('batch-from').value.replace(/-/g,'');
  const toVal = document.getElementById('batch-to').value.replace(/-/g,'');
  if(!fromVal||!toVal){ alert('開始日と終了日を選択してください'); return; }
  if(fromVal>toVal){ alert('開始日は終了日より前にしてください'); return; }
  if(evtSource){ evtSource.close(); evtSource=null; }
  document.getElementById('log').innerHTML='';
  document.getElementById('batch-progress').textContent='';
  setStatus('running','一括取得中...');
  const btn=document.getElementById('btn-batch');
  btn.disabled=true; btn.textContent='⏳ 取得中...';
  const weekendOnly=document.getElementById('chk-batch-weekend').checked;
  const deploy=document.getElementById('chk-batch-deploy').checked;
  const params=new URLSearchParams({from:fromVal,to:toVal,weekend_only:weekendOnly,deploy});
  evtSource=new EventSource('/api/batch_run?'+params);
  evtSource.onmessage=e=>{
    const line=JSON.parse(e.data);
    let cls='';
    if(line.startsWith('  ✓')) cls='ok';
    else if(line.startsWith('  ✗')||line.includes('エラー')) cls='err';
    else if(line.startsWith('===')||line.startsWith('[Step')) cls='head';
    else if(line.match(/^\[\\d+\\/\\d+\]/)) cls='info';
    const m=line.match(/\\[(\\d+)\\/(\\d+)\\]/);
    if(m) document.getElementById('batch-progress').textContent=`${m[1]} / ${m[2]} 日完了`;
    if(line==='__DONE__'){
      setStatus('done','一括取得完了');
      btn.disabled=false; btn.textContent='▶ 一括取得開始';
      evtSource.close(); evtSource=null;
    } else if(line==='__ERROR__'){
      setStatus('error','エラー');
      btn.disabled=false; btn.textContent='▶ 一括取得開始';
      evtSource.close(); evtSource=null;
    } else { appendLog(line,cls); }
  };
  evtSource.onerror=()=>{
    setStatus('error','接続エラー');
    btn.disabled=false; btn.textContent='▶ 一括取得開始';
    evtSource.close(); evtSource=null;
  };
}

function runWeekendUpdate(){
  const btn = document.getElementById('btn-weekend');
  btn.disabled=true; btn.textContent='⏳ 更新中...';
  document.getElementById('log').innerHTML='';
  setStatus('running','一括更新中...');
  if(evtSource){ evtSource.close(); evtSource=null; }
  evtSource = new EventSource('/api/weekend_update');
  evtSource.onmessage = e => {
    const line = JSON.parse(e.data);
    let cls='';
    if(line.includes('完了') || line.includes('✓')) cls='ok';
    else if(line.includes('ERROR') || line.includes('エラー')) cls='err';
    else if(line.startsWith('===') || line.startsWith('[')) cls='head';
    if(line==='__DONE__'){
      setStatus('done','一括更新完了');
      btn.disabled=false; btn.textContent='🔄 今週末を一括更新';
      evtSource.close(); evtSource=null;
    } else if(line==='__ERROR__'){
      setStatus('error','エラー');
      btn.disabled=false; btn.textContent='🔄 今週末を一括更新';
      evtSource.close(); evtSource=null;
    } else { appendLog(line, cls); }
  };
  evtSource.onerror = ()=>{
    setStatus('error','接続エラー');
    btn.disabled=false; btn.textContent='🔄 今週末を一括更新';
    evtSource.close(); evtSource=null;
  };
}
</script>
</body>
</html>
"""

_current_proc = None
_stop_flag = False


def get_db_info():
    db_path = os.path.join(BASE_DIR, 'cushion_db_full.json')
    try:
        with open(db_path, encoding='utf-8') as f:
            db = json.load(f)
        dates = sorted(k.split('_')[0] for k in db.keys() if '_' in k)
        return dates[0], dates[-1], len(db)
    except Exception:
        return '?', '?', 0


@app.route('/')
def index():
    d_min, d_max, d_count = get_db_info()
    html = ADMIN_HTML.replace(
        '<span id="db-range" style="color:#f59e0b;font-weight:700">読込中...</span>',
        f'<span id="db-range" style="color:#f59e0b;font-weight:700">{d_min} 〜 {d_max}</span>'
    ).replace(
        '<span id="db-count" style="color:#64748b"></span>',
        f'<span id="db-count" style="color:#64748b">({d_count}件)</span>'
    )
    return html


@app.route('/api/run')
def api_run():
    global _current_proc

    date_str = request.args.get('date', '')
    deploy = request.args.get('deploy', 'false') == 'true'
    no_scrape = request.args.get('no_scrape', 'false') == 'true'
    venue = request.args.get('venue', '')

    if not date_str or len(date_str) != 8:
        return Response('data: ' + json.dumps('日付エラー') + '\n\n', mimetype='text/event-stream')

    cmd = [sys.executable, '-u', '-X', 'utf8', os.path.join(BASE_DIR, 'pipeline.py'), date_str]
    if deploy:
        cmd.append('--deploy')
    if no_scrape:
        cmd.append('--no-scrape')
    if venue:
        cmd.extend(['--venue', venue])

    def generate():
        global _current_proc
        try:
            _current_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=BASE_DIR,
                encoding='utf-8',
                errors='replace',
            )
            for line in _current_proc.stdout:
                yield f'data: {json.dumps(line.rstrip())}\n\n'
            _current_proc.wait()
            rc = _current_proc.returncode
            _current_proc = None
            yield f'data: {json.dumps("__DONE__" if rc == 0 else "__ERROR__")}\n\n'
        except Exception as e:
            yield f'data: {json.dumps(str(e))}\n\n'
            yield f'data: {json.dumps("__ERROR__")}\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/weekend_update')
def api_weekend_update():
    """DB更新 → 今週土日のパイプライン再実行（no-scrape）"""
    import datetime as _dt
    today = _dt.date.today()
    # 今週の土曜・日曜を計算
    weekday = today.weekday()  # 0=月 … 5=土 6=日
    days_to_sat = (5 - weekday) % 7
    sat = today + _dt.timedelta(days=days_to_sat)
    sun = sat + _dt.timedelta(days=1)
    weekend_dates = [sat.strftime('%Y%m%d'), sun.strftime('%Y%m%d')]

    def generate():
        # Step1: DB更新
        yield f'data: {json.dumps("=== Step1: DB更新 ===")}\n\n'
        db_cmd = [sys.executable, '-u', '-X', 'utf8',
                  os.path.join(BASE_DIR, 'update_cushion_db.py')]
        proc = subprocess.Popen(db_cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, cwd=BASE_DIR,
                                encoding='utf-8', errors='replace')
        for line in proc.stdout:
            yield f'data: {json.dumps(line.rstrip())}\n\n'
        proc.wait()
        if proc.returncode != 0:
            yield f'data: {json.dumps("DB更新失敗")}\n\n'
            yield f'data: {json.dumps("__ERROR__")}\n\n'
            return

        # Step2: 土日パイプライン（出力ディレクトリが存在する日のみ）
        for date_str in weekend_dates:
            out_dir = os.path.join(BASE_DIR, 'output', date_str)
            if not os.path.exists(out_dir):
                yield f'data: {json.dumps(f"{date_str}: 出力なし（先にパイプラインを実行してください）")}\n\n'
                continue
            yield f'data: {json.dumps(f"=== Step2: {date_str} パイプライン ===")}\n\n'
            pip_cmd = [sys.executable, '-u', '-X', 'utf8',
                       os.path.join(BASE_DIR, 'pipeline.py'),
                       date_str, '--no-scrape', '--deploy']
            proc2 = subprocess.Popen(pip_cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, cwd=BASE_DIR,
                                     encoding='utf-8', errors='replace')
            for line in proc2.stdout:
                yield f'data: {json.dumps(line.rstrip())}\n\n'
            proc2.wait()

        yield f'data: {json.dumps("__DONE__")}\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/db_info')
def api_db_info():
    import json as _json
    db_path = os.path.join(BASE_DIR, 'cushion_db_full.json')
    try:
        with open(db_path, encoding='utf-8') as f:
            db = _json.load(f)
        dates = sorted(k.split('_')[0] for k in db.keys() if '_' in k)
        return {'min': dates[0], 'max': dates[-1], 'count': len(db)}
    except Exception as e:
        return {'min': '?', 'max': '?', 'count': 0}


@app.route('/api/update_db')
def api_update_db():
    with_year = request.args.get('with_year', 'false') == 'true'
    cmd = [sys.executable, '-u', '-X', 'utf8',
           os.path.join(BASE_DIR, 'update_cushion_db.py')]
    if with_year:
        import datetime as dt
        cmd.extend(['--year', str(dt.datetime.now().year)])

    def generate():
        global _current_proc
        try:
            _current_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=BASE_DIR, encoding='utf-8', errors='replace',
            )
            for line in _current_proc.stdout:
                yield f'data: {json.dumps(line.rstrip())}\n\n'
            _current_proc.wait()
            rc = _current_proc.returncode
            _current_proc = None
            yield f'data: {json.dumps("__DONE__" if rc == 0 else "__ERROR__")}\n\n'
        except Exception as e:
            yield f'data: {json.dumps(str(e))}\n\n'
            yield f'data: {json.dumps("__ERROR__")}\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/batch_run')
def api_batch_run():
    """日付範囲で過去データを一括取得"""
    import datetime as dt
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')
    weekend_only = request.args.get('weekend_only', 'true') == 'true'
    deploy = request.args.get('deploy', 'false') == 'true'

    try:
        d_from = dt.datetime.strptime(date_from, '%Y%m%d').date()
        d_to = dt.datetime.strptime(date_to, '%Y%m%d').date()
    except Exception:
        def err_gen():
            yield f'data: {json.dumps("日付形式エラー")}\n\n'
            yield f'data: {json.dumps("__ERROR__")}\n\n'
        return Response(err_gen(), mimetype='text/event-stream')

    dates = []
    d = d_from
    while d <= d_to:
        if not weekend_only or d.weekday() in (5, 6):
            dates.append(d.strftime('%Y%m%d'))
        d += dt.timedelta(days=1)

    def generate():
        global _current_proc, _stop_flag
        _stop_flag = False
        total = len(dates)
        yield f'data: {json.dumps(f"=== 一括取得開始: {total}日分 ===")}\n\n'
        done = 0
        for date_str in dates:
            if _stop_flag:
                yield f'data: {json.dumps("=== 停止しました ===")}\n\n'
                yield f'data: {json.dumps("__DONE__")}\n\n'
                return
            yield f'data: {json.dumps(f"[{done}/{total}] {date_str} 処理中...")}\n\n'
            cmd = [sys.executable, '-u', '-X', 'utf8',
                   os.path.join(BASE_DIR, 'pipeline.py'), date_str]
            if deploy:
                cmd.append('--deploy')
            try:
                _current_proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    cwd=BASE_DIR, encoding='utf-8', errors='replace'
                )
                for line in _current_proc.stdout:
                    if _stop_flag:
                        _current_proc.terminate()
                        break
                    yield f'data: {json.dumps(line.rstrip())}\n\n'
                _current_proc.wait()
                rc = _current_proc.returncode
                _current_proc = None
                if _stop_flag:
                    yield f'data: {json.dumps("=== 停止しました ===")}\n\n'
                    yield f'data: {json.dumps("__DONE__")}\n\n'
                    return
                done += 1
                status = '✓' if rc == 0 else '✗'
                msg = f"  {status} [{done}/{total}] {date_str} {'完了' if rc == 0 else 'エラー（スキップ）'}"
                yield f'data: {json.dumps(msg)}\n\n'
            except Exception as e:
                yield f'data: {json.dumps(f"  ✗ {date_str}: {e}")}\n\n'
                _current_proc = None
                done += 1
        yield f'data: {json.dumps(f"=== 完了: {done}/{total}日 ===")}\n\n'
        yield f'data: {json.dumps("__DONE__")}\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/stop')
def api_stop():
    global _current_proc, _stop_flag
    _stop_flag = True
    if _current_proc:
        _current_proc.terminate()
        _current_proc = None
    return 'ok'


if __name__ == '__main__':
    print('=' * 50)
    print('  管理サーバー起動中...')
    print('  ブラウザで開く: http://localhost:5000')
    print('  停止: Ctrl+C')
    print('=' * 50)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
