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
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0f172a; color: #e2e8f0; font-family: -apple-system, sans-serif; min-height: 100vh; }
.header {
  background: #1e293b; border-bottom: 1px solid #334155;
  padding: 16px 24px; display: flex; align-items: center; gap: 16px;
}
.header h1 { font-size: 18px; font-weight: 800; color: #f1f5f9; }
.header .badge {
  background: #f59e0b; color: #1e293b; font-size: 11px; font-weight: 700;
  padding: 2px 8px; border-radius: 4px;
}
.container { max-width: 800px; margin: 32px auto; padding: 0 16px; }

.card {
  background: #1e293b; border: 1px solid #334155; border-radius: 12px;
  padding: 24px; margin-bottom: 20px;
}
.card h2 { font-size: 14px; font-weight: 700; color: #94a3b8; margin-bottom: 16px; letter-spacing: 0.05em; text-transform: uppercase; }

.form-row { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 16px; }
.form-group { display: flex; flex-direction: column; gap: 6px; }
.form-group label { font-size: 12px; color: #94a3b8; font-weight: 600; }
.form-group input[type="date"], .form-group select {
  background: #0f172a; border: 1px solid #475569; border-radius: 8px;
  color: #f1f5f9; padding: 8px 12px; font-size: 14px; outline: none;
  transition: border-color 0.15s;
}
.form-group input[type="date"]:focus, .form-group select:focus {
  border-color: #f59e0b;
}

.checks { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
.check-label {
  display: flex; align-items: center; gap: 8px; cursor: pointer;
  font-size: 13px; color: #cbd5e1; user-select: none;
}
.check-label input[type="checkbox"] { accent-color: #f59e0b; width: 16px; height: 16px; }

.btn-run {
  background: #f59e0b; color: #1e293b; font-weight: 800; font-size: 15px;
  border: none; border-radius: 10px; padding: 12px 28px; cursor: pointer;
  transition: background 0.15s, transform 0.1s; white-space: nowrap;
}
.btn-run:hover { background: #fbbf24; }
.btn-run:active { transform: scale(0.97); }
.btn-run:disabled { background: #475569; color: #94a3b8; cursor: not-allowed; transform: none; }

.btn-open {
  background: transparent; border: 1px solid #475569; color: #94a3b8;
  font-size: 13px; border-radius: 8px; padding: 8px 16px; cursor: pointer;
  transition: border-color 0.15s, color 0.15s; text-decoration: none; display: inline-block;
}
.btn-open:hover { border-color: #f59e0b; color: #f59e0b; }

.log-area {
  background: #0f172a; border: 1px solid #334155; border-radius: 8px;
  padding: 16px; font-family: 'Menlo', 'Consolas', monospace; font-size: 12px;
  line-height: 1.7; height: 400px; overflow-y: auto; white-space: pre-wrap;
  color: #94a3b8;
}
.log-area .ok { color: #34d399; }
.log-area .err { color: #f87171; }
.log-area .head { color: #f59e0b; font-weight: 700; }
.log-area .info { color: #60a5fa; }

.status-bar {
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
  font-size: 12px; color: #64748b;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: #475569; }
.dot.running { background: #f59e0b; animation: pulse 1s infinite; }
.dot.done { background: #34d399; }
.dot.error { background: #f87171; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

.dates-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.date-chip {
  background: #0f172a; border: 1px solid #334155; border-radius: 8px;
  padding: 6px 12px; font-size: 12px; color: #cbd5e1; cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.date-chip:hover { border-color: #f59e0b; color: #f59e0b; }

a.site-link { color: #f59e0b; text-decoration: none; font-size: 13px; }
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

  <!-- クイック日付 -->
  <div class="card">
    <h2>クイック選択</h2>
    <div class="dates-grid" id="quick-dates"></div>
  </div>

</div>

<script>
let evtSource = null;

// 今日の日付をデフォルトに
const today = new Date();
const pad = n => String(n).padStart(2,'0');
document.getElementById('date-input').value =
  `${today.getFullYear()}-${pad(today.getMonth()+1)}-${pad(today.getDate())}`;

// クイック日付（過去7日）
const qdiv = document.getElementById('quick-dates');
for(let i=0; i<7; i++){
  const d = new Date(); d.setDate(d.getDate()-i);
  const label = i===0?'今日':i===1?'昨日':`${d.getMonth()+1}/${d.getDate()}`;
  const val = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
  const chip = document.createElement('div');
  chip.className='date-chip';
  chip.textContent=label;
  chip.onclick=()=>{ document.getElementById('date-input').value=val; };
  qdiv.appendChild(chip);
}

function setStatus(state, text){
  const dot = document.getElementById('status-dot');
  dot.className='dot '+(state||'');
  document.getElementById('status-text').textContent=text;
}

function appendLog(text, cls){
  const log = document.getElementById('log');
  const span = document.createElement('span');
  if(cls) span.className=cls;
  span.textContent=text+'\n';
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
}
</script>
</body>
</html>
"""

_current_proc = None


@app.route('/')
def index():
    return render_template_string(ADMIN_HTML)


@app.route('/api/run')
def api_run():
    global _current_proc

    date_str = request.args.get('date', '')
    deploy = request.args.get('deploy', 'false') == 'true'
    no_scrape = request.args.get('no_scrape', 'false') == 'true'
    venue = request.args.get('venue', '')

    if not date_str or len(date_str) != 8:
        return Response('data: ' + json.dumps('日付エラー') + '\n\n', mimetype='text/event-stream')

    cmd = [sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, 'pipeline.py'), date_str]
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


@app.route('/api/stop')
def api_stop():
    global _current_proc
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
