#!/usr/bin/env python3
"""
keiba-scatter-v2 管理ダッシュボード
起動: python admin.py  または  start_admin.bat
アクセス: http://localhost:5001/
"""

from flask import Flask, Response, request, jsonify, stream_with_context
import subprocess, json, os, queue, threading, re, csv, io
from datetime import datetime

app = Flask(__name__)

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
CUSHION_DB_PATH = os.path.join(BASE_DIR, 'cushion_db_full.json')
DEPLOY_CONFIG_PATH = os.path.join(BASE_DIR, 'deploy_config.json')
OUTPUT_DIR      = os.path.join(BASE_DIR, 'output')
DATA_DIR        = os.path.join(BASE_DIR, 'data')
OBS_CSV_PATH    = os.path.join(DATA_DIR, 'observations.csv')

OBS_FIELDS = [
    'id', 'date', 'venue', 'venue_ja', 'surface',
    'cushion_value', 'moisture_rate', 'measurement_time',
    'temperature_avg', 'humidity_avg', 'wind_speed_avg',
    'rainfall_24h', 'rainfall_prev_week',
    'watering', 'watering_notes', 'maintenance_notes',
    'weather', 'track_condition', 'source_url', 'updated_at', 'notes',
]

VENUE_MAP = {
    'nakayama':  '中山', 'hanshin':  '阪神', 'tokyo':    '東京',
    'kyoto':     '京都', 'chukyo':   '中京', 'fukushima':'福島',
    'kokura':    '小倉', 'niigata':  '新潟', 'sapporo':  '札幌',
    'hakodate':  '函館',
}

current_job = {'process': None, 'queue': queue.Queue(), 'running': False}


# ── Observations CSV helpers ──

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(OBS_CSV_PATH):
        with open(OBS_CSV_PATH, 'w', encoding='utf-8', newline='') as f:
            csv.DictWriter(f, fieldnames=OBS_FIELDS).writeheader()

def read_obs():
    ensure_data_dir()
    with open(OBS_CSV_PATH, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))

def write_obs(rows):
    ensure_data_dir()
    with open(OBS_CSV_PATH, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=OBS_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, '') for k in OBS_FIELDS})


# ── Status ──

def get_status():
    db_count = 0
    if os.path.exists(CUSHION_DB_PATH):
        with open(CUSHION_DB_PATH, encoding='utf-8') as f:
            db_count = len(json.load(f))

    obs_count = len(read_obs()) if os.path.exists(OBS_CSV_PATH) else 0

    repo = pages_url = ''
    if os.path.exists(DEPLOY_CONFIG_PATH):
        with open(DEPLOY_CONFIG_PATH, encoding='utf-8') as f:
            config = json.load(f)
            repo = config.get('repo', '')
            if '/' in repo:
                owner, name = repo.split('/', 1)
                pages_url = f'https://{owner}.github.io/{name}/'

    output_dates = []
    if os.path.exists(OUTPUT_DIR):
        dirs = [d for d in os.listdir(OUTPUT_DIR)
                if os.path.isdir(os.path.join(OUTPUT_DIR, d)) and re.match(r'^\d{8}$', d)]
        output_dates = sorted(dirs, reverse=True)[:10]

    return {
        'db_count':     db_count,
        'obs_count':    obs_count,
        'repo':         repo,
        'pages_url':    pages_url,
        'output_dates': output_dates,
        'today':        datetime.now().strftime('%Y%m%d'),
        'running':      current_job['running'],
    }


# ── Routes: pipeline ──

@app.route('/')
def index():
    return HTML_TEMPLATE

@app.route('/api/status')
def api_status():
    return jsonify(get_status())

@app.route('/api/run', methods=['POST'])
def api_run():
    if current_job['running']:
        return jsonify({'error': '既に実行中です'}), 400
    data = request.json
    date = data.get('date', '').strip()
    if not re.match(r'^\d{8}$', date):
        return jsonify({'error': '日付形式が不正です (YYYYMMDD)'}), 400
    cmd = ['python', 'pipeline.py', date]
    if data.get('venue'):      cmd += ['--venue', data['venue']]
    if data.get('race'):       cmd += ['--race', str(data['race'])]
    if data.get('no_scrape'):  cmd += ['--no-scrape']
    if data.get('manual'):     cmd += ['--manual']
    if data.get('force_update'): cmd += ['--force-update']
    if data.get('deploy'):     cmd += ['--deploy']
    if data.get('cleanup'):    cmd += ['--cleanup']
    _start_job(cmd)
    return jsonify({'ok': True, 'cmd': ' '.join(cmd)})

@app.route('/api/update-db', methods=['POST'])
def api_update_db():
    if current_job['running']:
        return jsonify({'error': '既に実行中です'}), 400
    _start_job(['python', 'update_cushion_db.py'])
    return jsonify({'ok': True})

@app.route('/api/deploy-only', methods=['POST'])
def api_deploy_only():
    if current_job['running']:
        return jsonify({'error': '既に実行中です'}), 400
    data = request.json
    date = data.get('date', '').strip()
    if not re.match(r'^\d{8}$', date):
        return jsonify({'error': '日付形式が不正です'}), 400
    cmd = ['python', 'pipeline.py', date, '--no-scrape', '--deploy']
    if data.get('cleanup'): cmd += ['--cleanup']
    _start_job(cmd)
    return jsonify({'ok': True, 'cmd': ' '.join(cmd)})

@app.route('/api/weekend-scrape', methods=['POST'])
def api_weekend_scrape():
    global current_job
    if current_job['running']:
        return jsonify({'error': '既に実行中です'}), 400
    import datetime as _dt
    today = _dt.date.today()
    days_to_sat = (5 - today.weekday()) % 7
    sat = today + _dt.timedelta(days=days_to_sat)
    sun = sat + _dt.timedelta(days=1)
    cmds = [
        ['python', 'pipeline.py', sat.strftime('%Y%m%d'), '--deploy'],
        ['python', 'pipeline.py', sun.strftime('%Y%m%d'), '--deploy'],
    ]

    current_job['queue'] = queue.Queue()
    current_job['running'] = True

    def run():
        try:
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUTF8'] = '1'
            env['PYTHONUNBUFFERED'] = '1'
            for cmd in cmds:
                run_cmd = [cmd[0], '-u'] + cmd[1:]
                current_job['queue'].put(f'=== {cmd[2]} フルスクレイピング ===')
                proc = subprocess.Popen(
                    run_cmd, cwd=BASE_DIR,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    encoding='utf-8', errors='replace', bufsize=1, env=env,
                )
                current_job['process'] = proc
                for line in proc.stdout:
                    current_job['queue'].put(line.rstrip())
                proc.wait()
                if proc.returncode != 0:
                    current_job['queue'].put(f'__EXIT__{proc.returncode}')
                    return
            current_job['queue'].put('=== 今週末一括取得完了 ===')
            current_job['queue'].put('__EXIT__0')
        except Exception as e:
            current_job['queue'].put(f'エラー: {e}')
            current_job['queue'].put('__EXIT__1')
        finally:
            current_job['running'] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    if current_job['process']:
        try: current_job['process'].terminate()
        except Exception: pass
    current_job['running'] = False
    current_job['queue'].put('__EXIT__-1')
    return jsonify({'ok': True})

@app.route('/stream')
def stream():
    def generate():
        while True:
            try:
                line = current_job['queue'].get(timeout=30)
                yield f'data: {json.dumps(line, ensure_ascii=False)}\n\n'
                if line.startswith('__EXIT__'): break
            except queue.Empty:
                yield 'data: "__PING__"\n\n'
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ── Routes: observations ──

@app.route('/api/obs', methods=['GET'])
def api_obs_list():
    rows = read_obs()
    rows.sort(key=lambda r: (r.get('date', ''), r.get('venue', '')), reverse=True)
    return jsonify(rows)

@app.route('/api/obs', methods=['POST'])
def api_obs_create():
    data = request.json or {}
    venue      = data.get('venue', '').strip()
    surface    = data.get('surface', 'turf').strip()
    obs_date   = data.get('date', '').strip()
    if not obs_date or not venue:
        return jsonify({'error': '日付と会場は必須です'}), 400

    row_id = f"{obs_date.replace('-', '')}_{venue}_{surface}"
    rows = read_obs()
    if any(r.get('id') == row_id for r in rows):
        return jsonify({'error': f'ID {row_id} は既に存在します。編集してください'}), 409

    new_row = {k: '' for k in OBS_FIELDS}
    for k, v in data.items():
        if k in OBS_FIELDS:
            new_row[k] = str(v) if v is not None else ''
    new_row['id']        = row_id
    new_row['venue_ja']  = VENUE_MAP.get(venue, venue)
    new_row['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rows.append(new_row)
    write_obs(rows)
    return jsonify({'ok': True, 'id': row_id})

VENUE_MAP_REVERSE = {v: k for k, v in VENUE_MAP.items()}

@app.route('/api/fetch-jra', methods=['POST'])
def api_fetch_jra():
    try:
        import sys as _sys
        _sys.path.insert(0, BASE_DIR)
        from update_cushion_db import fetch_jra_live_history
        records = fetch_jra_live_history()
    except Exception as e:
        return jsonify({'error': f'スクレイピングエラー: {e}'}), 500

    if not records:
        return jsonify({'ok': True, 'created': 0, 'skipped': 0, 'message': 'JRAからデータが取得できませんでした'})

    data = request.json or {}
    filter_date = data.get('date', '')  # YYYY-MM-DD、空なら全日程

    rows = read_obs()
    existing_ids = {r.get('id') for r in rows}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    created = skipped = 0
    fetched_dates = set()

    for rec in records:
        # rec['date'] は 'YYYY/MM/DD' 形式
        obs_date = rec['date'].replace('/', '-')
        if filter_date and obs_date != filter_date:
            continue
        fetched_dates.add(obs_date)

        venue_ja = rec.get('venue', '')
        venue_en = VENUE_MAP_REVERSE.get(venue_ja)
        if not venue_en:
            continue

        cushion = rec.get('cushion')
        turf_mo = rec.get('turf_goal')
        dirt_mo = rec.get('dirt_goal')

        # 芝レコード
        if cushion is not None or turf_mo is not None:
            rid = f"{obs_date.replace('-','')}_{venue_en}_turf"
            if rid not in existing_ids:
                r = {k: '' for k in OBS_FIELDS}
                r.update({'id': rid, 'date': obs_date, 'venue': venue_en, 'venue_ja': venue_ja,
                          'surface': 'turf',
                          'cushion_value': str(cushion) if cushion is not None else '',
                          'moisture_rate': str(turf_mo) if turf_mo is not None else '',
                          'updated_at': now})
                rows.append(r); existing_ids.add(rid); created += 1
            else:
                skipped += 1

        # ダートレコード
        if dirt_mo is not None:
            rid = f"{obs_date.replace('-','')}_{venue_en}_dirt"
            if rid not in existing_ids:
                r = {k: '' for k in OBS_FIELDS}
                r.update({'id': rid, 'date': obs_date, 'venue': venue_en, 'venue_ja': venue_ja,
                          'surface': 'dirt', 'cushion_value': '',
                          'moisture_rate': str(dirt_mo) if dirt_mo is not None else '',
                          'updated_at': now})
                rows.append(r); existing_ids.add(rid); created += 1
            else:
                skipped += 1

    if created > 0:
        write_obs(rows)
    return jsonify({'ok': True, 'created': created, 'skipped': skipped,
                    'dates': sorted(fetched_dates)})


@app.route('/api/fetch-jra-pdf', methods=['POST'])
def api_fetch_jra_pdf():
    import sys as _sys
    _sys.path.insert(0, BASE_DIR)
    req = request.json or {}
    year_from = int(req.get('year_from', datetime.now().year))
    year_to   = int(req.get('year_to',   datetime.now().year))
    years = list(range(year_from, year_to + 1))

    try:
        from update_cushion_db import fetch_pdf, parse_cushion_pdf, parse_cushion_pdf_legacy, fetch_jra_live_history
        import time as _time
    except Exception as e:
        return jsonify({'error': f'インポートエラー: {e}'}), 500

    rows = read_obs()
    existing_ids = {r.get('id') for r in rows}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    created = skipped = 0

    def add_record(obs_date, venue_en, venue_ja, surface, cushion, moisture):
        nonlocal created, skipped
        rid = f"{obs_date.replace('-','')}_{venue_en}_{surface}"
        if rid in existing_ids:
            skipped += 1
            return
        r = {k: '' for k in OBS_FIELDS}
        r.update({'id': rid, 'date': obs_date, 'venue': venue_en, 'venue_ja': venue_ja,
                  'surface': surface,
                  'cushion_value': str(cushion) if cushion is not None else '',
                  'moisture_rate': str(moisture) if moisture is not None else '',
                  'updated_at': now})
        rows.append(r)
        existing_ids.add(rid)
        created += 1

    # ライブデータ（今週分）
    try:
        live = fetch_jra_live_history()
        for rec in live:
            obs_date = rec['date'].replace('/', '-')
            venue_ja = rec.get('venue', '')
            venue_en = VENUE_MAP_REVERSE.get(venue_ja)
            if not venue_en:
                continue
            if rec.get('cushion') is not None or rec.get('turf_goal') is not None:
                add_record(obs_date, venue_en, venue_ja, 'turf', rec.get('cushion'), rec.get('turf_goal'))
            if rec.get('dirt_goal') is not None:
                add_record(obs_date, venue_en, venue_ja, 'dirt', None, rec.get('dirt_goal'))
    except Exception:
        pass

    # PDFアーカイブ（過去データ）
    from update_cushion_db import VENUE_MAP as PDF_VENUE_MAP
    for year in years:
        for venue_en, venue_ja in PDF_VENUE_MAP.items():
            for kai in range(1, 7):
                try:
                    pdf_bytes = fetch_pdf(year, venue_en, kai)
                    if pdf_bytes is None:
                        continue
                    records = parse_cushion_pdf(pdf_bytes)
                    if not records:
                        records = parse_cushion_pdf_legacy(pdf_bytes, venue_en)
                    for rec in records:
                        obs_date = rec['date'].replace('/', '-')
                        add_record(obs_date, venue_en, venue_ja, 'turf', rec.get('cushion'), rec.get('turf_goal'))
                        if rec.get('dirt_goal') is not None:
                            add_record(obs_date, venue_en, venue_ja, 'dirt', None, rec.get('dirt_goal'))
                    _time.sleep(0.3)
                except Exception:
                    continue

    if created > 0:
        write_obs(rows)
    return jsonify({'ok': True, 'created': created, 'skipped': skipped})


@app.route('/api/fetch-weather', methods=['POST'])
def api_fetch_weather():
    try:
        import sys as _sys
        _sys.path.insert(0, BASE_DIR)
        from fetch_weather import fill_weather
    except Exception as e:
        return jsonify({'error': f'インポートエラー: {e}'}), 500

    rows = read_obs()
    try:
        updated = fill_weather(rows)
    except Exception as e:
        return jsonify({'error': f'気象取得エラー: {e}'}), 500

    if updated > 0:
        write_obs(rows)
    return jsonify({'ok': True, 'updated': updated})


@app.route('/api/obs/bulk', methods=['POST'])
def api_obs_bulk():
    items = request.json or []
    rows = read_obs()
    existing_ids = {r.get('id') for r in rows}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    created = skipped = 0
    for item in items:
        venue    = item.get('venue', '').strip()
        surface  = item.get('surface', '').strip()
        obs_date = item.get('date', '').strip()
        if not venue or not obs_date:
            continue
        row_id = f"{obs_date.replace('-', '')}_{venue}_{surface}"
        if row_id in existing_ids:
            skipped += 1
            continue
        new_row = {k: '' for k in OBS_FIELDS}
        for k, v in item.items():
            if k in OBS_FIELDS:
                new_row[k] = str(v) if v is not None else ''
        new_row['id']        = row_id
        new_row['venue_ja']  = VENUE_MAP.get(venue, venue)
        new_row['updated_at'] = now
        rows.append(new_row)
        existing_ids.add(row_id)
        created += 1
    write_obs(rows)
    return jsonify({'ok': True, 'created': created, 'skipped': skipped})

@app.route('/api/obs/import', methods=['POST'])
def api_obs_import():
    if 'file' not in request.files:
        return jsonify({'error': 'ファイルがありません'}), 400
    file = request.files['file']
    content = file.read().decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(content))
    rows = read_obs()
    existing_ids = {r.get('id') for r in rows}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    created = skipped = errors = 0
    for row in reader:
        row_id = row.get('id', '').strip()
        if not row_id:
            venue    = row.get('venue', '').strip()
            surface  = row.get('surface', '').strip()
            obs_date = row.get('date', '').strip()
            if not venue or not obs_date:
                errors += 1
                continue
            row_id = f"{obs_date.replace('-', '')}_{venue}_{surface}"
        if row_id in existing_ids:
            skipped += 1
            continue
        new_row = {k: row.get(k, '') for k in OBS_FIELDS}
        new_row['id']       = row_id
        new_row['venue_ja'] = new_row.get('venue_ja') or VENUE_MAP.get(new_row.get('venue', ''), new_row.get('venue', ''))
        new_row['updated_at'] = new_row.get('updated_at') or now
        rows.append(new_row)
        existing_ids.add(row_id)
        created += 1
    write_obs(rows)
    return jsonify({'ok': True, 'created': created, 'skipped': skipped, 'errors': errors})

@app.route('/api/obs/template')
def api_obs_template():
    lines = [','.join(OBS_FIELDS),
             '20260418,nakayama,中山,turf,9.6,10.6,09:00,,,,,,0,,,晴,良,,2026-04-18 09:00:00,',
             '20260418,nakayama,中山,dirt,,8.2,09:00,,,,,,0,,,晴,良,,2026-04-18 09:00:00,']
    return Response('\n'.join(lines) + '\n', mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=obs_template.csv'})

@app.route('/api/obs/export')
def api_obs_export():
    ensure_data_dir()
    if not os.path.exists(OBS_CSV_PATH):
        return '該当なし', 404
    with open(OBS_CSV_PATH, encoding='utf-8') as f:
        content = f.read()
    fname = f'observations_{datetime.now().strftime("%Y%m%d")}.csv'
    return Response(
        content, mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={fname}'}
    )

@app.route('/api/obs/<path:obs_id>', methods=['PUT'])
def api_obs_update(obs_id):
    data = request.json or {}
    rows = read_obs()
    for i, row in enumerate(rows):
        if row.get('id') == obs_id:
            for k, v in data.items():
                if k in OBS_FIELDS and k not in ('id', 'venue_ja', 'updated_at'):
                    rows[i][k] = str(v) if v is not None else ''
            rows[i]['venue_ja']   = VENUE_MAP.get(rows[i].get('venue', ''), rows[i].get('venue', ''))
            rows[i]['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            write_obs(rows)
            return jsonify({'ok': True})
    return jsonify({'error': '見つかりません'}), 404

@app.route('/api/obs/<path:obs_id>', methods=['DELETE'])
def api_obs_delete(obs_id):
    rows = read_obs()
    new_rows = [r for r in rows if r.get('id') != obs_id]
    if len(new_rows) == len(rows):
        return jsonify({'error': '見つかりません'}), 404
    write_obs(new_rows)
    return jsonify({'ok': True})


def _start_job(cmd):
    global current_job
    current_job['queue'] = queue.Queue()
    current_job['running'] = True

    def run():
        try:
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUTF8'] = '1'
            env['PYTHONUNBUFFERED'] = '1'
            # -u: 標準出力をアンバッファードにして即時ストリーミング
            run_cmd = cmd[:]
            if run_cmd[0] == 'python':
                run_cmd.insert(1, '-u')
            proc = subprocess.Popen(
                run_cmd, cwd=BASE_DIR,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding='utf-8', errors='replace', bufsize=1, env=env,
            )
            current_job['process'] = proc
            for line in proc.stdout:
                current_job['queue'].put(line.rstrip())
            proc.wait()
            current_job['queue'].put(f'__EXIT__{proc.returncode}')
        except Exception as e:
            current_job['queue'].put(f'エラー: {e}')
            current_job['queue'].put('__EXIT__1')
        finally:
            current_job['running'] = False

    threading.Thread(target=run, daemon=True).start()


# ════════════════════════════════════════════════════════
#  HTML TEMPLATE
# ════════════════════════════════════════════════════════

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>管理ダッシュボード — keiba-scatter-v2</title>
<style>
:root {
  --bg: #0a0f1e; --bg-card: #111827; --bg-card2: #1e293b;
  --bg-input: #0f172a; --border: #1e3a5f; --border2: #334155;
  --text: #f1f5f9; --text-sub: #94a3b8; --text-muted: #475569;
  --accent: #f59e0b; --accent2: #38bdf8; --green: #22c55e;
  --red: #ef4444; --purple: #a78bfa;
  --font-mono: 'JetBrains Mono', 'Consolas', 'Courier New', monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html { font-size: 14px; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Noto Sans JP', sans-serif;
  background: var(--bg); color: var(--text);
  min-height: 100vh; -webkit-font-smoothing: antialiased;
}

/* ── Nav ── */
.nav {
  background: var(--bg-card); border-bottom: 1px solid var(--border);
  padding: 0 20px; height: 52px;
  display: flex; align-items: center; justify-content: space-between;
  position: sticky; top: 0; z-index: 100;
  box-shadow: 0 2px 16px rgba(0,0,0,0.5);
}
.nav-title {
  font-size: 15px; font-weight: 900; letter-spacing: -0.3px;
  display: flex; align-items: center; gap: 10px;
}
.nav-title .icon {
  width: 28px; height: 28px; background: var(--accent);
  border-radius: 7px; display: flex; align-items: center; justify-content: center;
  font-size: 14px;
}
.nav-badge {
  font-size: 10px; font-weight: 700;
  background: var(--bg-input); border: 1px solid var(--border2);
  color: var(--text-sub); padding: 2px 8px; border-radius: 20px;
}
.nav-actions { display: flex; gap: 8px; align-items: center; }
.nav-link {
  font-size: 12px; font-weight: 700; color: var(--text-sub);
  text-decoration: none; padding: 5px 10px; border-radius: 6px;
  border: 1px solid var(--border2); transition: all 0.15s;
}
.nav-link:hover { color: var(--accent2); border-color: var(--accent2); }

/* ── Container ── */
.container { max-width: 1200px; margin: 0 auto; padding: 20px 16px; }

/* ── Status cards ── */
.status-row {
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px;
  margin-bottom: 16px;
}
@media (min-width: 640px) { .status-row { grid-template-columns: repeat(4, 1fr); } }
.stat-card {
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 12px; padding: 14px 16px;
}
.stat-label { font-size: 10px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
.stat-value { font-size: 22px; font-weight: 900; margin-top: 4px; color: var(--text); font-family: var(--font-mono); }
.stat-value.green  { color: var(--green); }
.stat-value.amber  { color: var(--accent); }
.stat-value.blue   { color: var(--accent2); }
.stat-value.purple { color: var(--purple); }
.stat-sub { font-size: 10px; color: var(--text-muted); margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* ── Tab nav ── */
.tab-nav {
  display: flex; gap: 4px; margin-bottom: 16px;
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 12px; padding: 5px;
}
.tab-btn {
  flex: 1; padding: 9px 16px; border-radius: 8px; border: none;
  background: none; color: var(--text-muted);
  font-size: 13px; font-weight: 700; cursor: pointer;
  transition: all 0.15s; -webkit-tap-highlight-color: transparent;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { background: var(--bg-card2); color: var(--accent); }

/* ── Grid layouts ── */
.grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
@media (min-width: 900px) { .grid { grid-template-columns: 380px 1fr; } }
.obs-grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
@media (min-width: 900px) { .obs-grid { grid-template-columns: 400px 1fr; } }

/* ── Card ── */
.card {
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 14px; overflow: hidden;
}
.card-header {
  padding: 13px 16px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
  background: var(--bg-card2);
}
.card-header h2 { font-size: 13px; font-weight: 800; color: var(--text); display: flex; align-items: center; gap: 7px; }
.card-body { padding: 16px; }

/* ── Section title inside form ── */
.section-title {
  font-size: 10px; font-weight: 800; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.6px;
  margin: 14px 0 10px; padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}
.section-title:first-child { margin-top: 0; }

/* ── Form ── */
.form-group { margin-bottom: 12px; }
.form-label {
  display: block; font-size: 11px; font-weight: 700; color: var(--text-sub);
  text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 6px;
}
.form-input {
  width: 100%; background: var(--bg-input); border: 1px solid var(--border2);
  border-radius: 8px; padding: 9px 12px; color: var(--text);
  font-size: 14px; font-family: var(--font-mono); font-weight: 600;
  outline: none; transition: border-color 0.15s;
}
.form-input:focus { border-color: var(--accent); }
.form-input::placeholder { color: var(--text-muted); font-weight: 400; }
select.form-input { cursor: pointer; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.form-row-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }

/* ── Surface toggle ── */
.surface-group { display: flex; gap: 4px; }
.surf-btn {
  flex: 1; padding: 9px; border-radius: 8px;
  border: 1px solid var(--border2); background: var(--bg-input);
  color: var(--text-muted); font-size: 13px; font-weight: 800;
  cursor: pointer; transition: all 0.12s; text-align: center;
}
.surf-btn.active-turf { background: rgba(34,197,94,0.15); border-color: #4ade80; color: #4ade80; }
.surf-btn.active-dirt { background: rgba(245,158,11,0.15); border-color: #fbbf24; color: #fbbf24; }

/* ── Collapsible ── */
details.collapsible {
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 10px; margin-bottom: 10px;
}
details.collapsible summary {
  padding: 10px 14px; font-size: 12px; font-weight: 700; color: var(--text-sub);
  cursor: pointer; list-style: none; display: flex; align-items: center; gap: 6px;
  user-select: none;
}
details.collapsible summary::before { content: '▶'; font-size: 9px; transition: transform 0.15s; }
details[open].collapsible summary::before { transform: rotate(90deg); }
details.collapsible .detail-body { padding: 0 14px 14px; }

/* ── Checkbox toggle ── */
.checkbox-group { display: flex; flex-direction: column; gap: 8px; }
.checkbox-item {
  display: flex; align-items: center; gap: 10px; cursor: pointer;
  padding: 8px 10px; border-radius: 8px; border: 1px solid transparent;
  transition: all 0.12s; -webkit-tap-highlight-color: transparent;
}
.checkbox-item:hover { background: var(--bg-card2); border-color: var(--border2); }
.checkbox-item input[type=checkbox] { display: none; }
.cb-box {
  width: 18px; height: 18px; border: 2px solid var(--border2); border-radius: 5px;
  flex-shrink: 0; display: flex; align-items: center; justify-content: center;
  transition: all 0.12s;
}
.checkbox-item.checked .cb-box { background: var(--accent); border-color: var(--accent); }
.cb-check { color: #000; font-size: 11px; font-weight: 900; display: none; }
.checkbox-item.checked .cb-check { display: block; }
.cb-label { font-size: 13px; font-weight: 600; color: var(--text); }
.cb-desc { font-size: 10px; color: var(--text-muted); margin-left: auto; }

/* ── Buttons ── */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 10px 18px; border-radius: 9px; font-size: 13px; font-weight: 800;
  cursor: pointer; border: none; transition: all 0.15s;
  -webkit-tap-highlight-color: transparent;
}
.btn:active { transform: scale(0.97); }
.btn-primary   { background: var(--accent); color: #000; }
.btn-primary:hover { background: #fbbf24; }
.btn-primary:disabled { background: var(--text-muted); color: var(--bg-card); cursor: not-allowed; transform: none; }
.btn-danger    { background: var(--red); color: #fff; }
.btn-danger:hover { background: #f87171; }
.btn-secondary { background: var(--bg-card2); color: var(--text); border: 1px solid var(--border2); }
.btn-secondary:hover { border-color: var(--accent2); color: var(--accent2); }
.btn-green     { background: var(--green); color: #000; }
.btn-green:hover { background: #4ade80; }
.btn-purple    { background: var(--purple); color: #000; }
.btn-purple:hover { background: #c4b5fd; }
.btn-full  { width: 100%; }
.btn-group { display: flex; gap: 8px; margin-top: 14px; }
.btn-sm {
  font-size: 10px; font-weight: 700; padding: 4px 8px; border-radius: 5px;
  border: 1px solid var(--border2); background: var(--bg-card2); color: var(--text-sub);
  cursor: pointer; transition: all 0.12s;
}
.btn-sm:hover { border-color: var(--accent); color: var(--accent); }
.btn-sm.del:hover { border-color: var(--red); color: var(--red); }

/* ── Terminal ── */
.terminal-wrap {
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 14px; overflow: hidden;
  display: flex; flex-direction: column;
  height: 100%; min-height: 400px;
}
.terminal-header {
  background: var(--bg-card2); border-bottom: 1px solid var(--border);
  padding: 11px 16px; display: flex; align-items: center; gap: 10px; flex-shrink: 0;
}
.terminal-dots { display: flex; gap: 6px; }
.terminal-dots span { width: 12px; height: 12px; border-radius: 50%; }
.td-red    { background: #ff5f57; }
.td-yellow { background: #ffbd2e; }
.td-green  { background: #28ca41; }
.terminal-title { font-size: 12px; font-weight: 700; color: var(--text-muted); font-family: var(--font-mono); }
.terminal-status { margin-left: auto; }
.status-dot {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11px; font-weight: 700; color: var(--text-muted);
}
.status-dot .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--border2); }
.status-dot.running .dot { background: var(--green); animation: pulse 1s infinite; }
.status-dot.running { color: var(--green); }
.status-dot.success .dot { background: var(--green); }
.status-dot.success { color: var(--green); }
.status-dot.error   .dot { background: var(--red); }
.status-dot.error        { color: var(--red); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
.terminal-body {
  flex: 1; overflow-y: auto; padding: 14px 16px;
  font-family: var(--font-mono); font-size: 12px; line-height: 1.7;
  background: #060c18; scroll-behavior: smooth;
}
.terminal-body::-webkit-scrollbar { width: 4px; }
.terminal-body::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }
.log-line          { color: #cbd5e1; white-space: pre-wrap; word-break: break-all; }
.log-line.ok       { color: #4ade80; }
.log-line.ng       { color: #f87171; }
.log-line.step     { color: var(--accent); font-weight: 700; }
.log-line.sep      { color: var(--border2); }
.log-line.deploy   { color: var(--accent2); }
.log-line.warn     { color: #fbbf24; }
.log-line.exit-ok  { color: var(--green); font-weight: 700; }
.log-line.exit-ng  { color: var(--red); font-weight: 700; }
.terminal-footer {
  background: var(--bg-card2); border-top: 1px solid var(--border);
  padding: 8px 14px; display: flex; align-items: center; justify-content: space-between;
  flex-shrink: 0;
}
.log-count { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }
.btn-clear { font-size: 11px; font-weight: 700; color: var(--text-muted); background: none; border: none; cursor: pointer; padding: 3px 8px; border-radius: 5px; }
.btn-clear:hover { color: var(--text); background: var(--border); }

/* ── History list ── */
.history-list { display: flex; flex-direction: column; gap: 6px; }
.history-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px; background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 8px; transition: border-color 0.12s;
}
.history-item:hover { border-color: var(--accent); }
.history-date  { font-size: 13px; font-weight: 700; font-family: var(--font-mono); color: var(--accent2); }
.history-meta  { font-size: 11px; color: var(--text-muted); }
.history-actions { display: flex; gap: 5px; }

/* ── Quick actions ── */
.quick-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.quick-btn {
  background: var(--bg-input); border: 1px solid var(--border2);
  border-radius: 10px; padding: 12px 14px; cursor: pointer; transition: all 0.12s;
  text-align: left; -webkit-tap-highlight-color: transparent;
}
.quick-btn:hover { border-color: var(--accent); background: var(--bg-card2); }
.quick-btn:active { transform: scale(0.97); }
.quick-btn .qb-icon  { font-size: 18px; margin-bottom: 4px; }
.quick-btn .qb-label { font-size: 12px; font-weight: 800; color: var(--text); }
.quick-btn .qb-desc  { font-size: 10px; color: var(--text-muted); margin-top: 2px; }

/* ── Obs list table ── */
.obs-list { display: flex; flex-direction: column; gap: 6px; }
.obs-row {
  display: grid; grid-template-columns: auto auto 1fr auto auto auto;
  align-items: center; gap: 8px;
  padding: 9px 12px; background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 8px; transition: border-color 0.12s;
}
.obs-row:hover { border-color: var(--accent2); }
.obs-date   { font-size: 12px; font-weight: 700; font-family: var(--font-mono); color: var(--accent2); white-space: nowrap; }
.obs-venue  { font-size: 12px; font-weight: 700; color: var(--text); }
.obs-surf   { font-size: 10px; font-weight: 800; padding: 2px 6px; border-radius: 5px; }
.obs-surf.turf { background: rgba(34,197,94,0.15); color: #4ade80; border: 1px solid rgba(34,197,94,0.3); }
.obs-surf.dirt { background: rgba(245,158,11,0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3); }
.obs-cv     { font-size: 12px; font-family: var(--font-mono); color: var(--text-sub); white-space: nowrap; }
.obs-warn   { color: var(--red) !important; }
.obs-actions { display: flex; gap: 4px; }

/* ── Edit mode indicator ── */
.edit-mode-bar {
  background: rgba(167,139,250,0.1); border: 1px solid var(--purple);
  border-radius: 8px; padding: 8px 12px; margin-bottom: 14px;
  display: flex; align-items: center; justify-content: space-between;
  font-size: 12px; font-weight: 700; color: var(--purple);
}

/* ── Bulk table ── */
.bulk-table { width: 100%; border-collapse: collapse; font-size: 12px; min-width: 560px; }
.bulk-table th {
  padding: 8px 10px; background: var(--bg-card2);
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  border-bottom: 2px solid var(--border); white-space: nowrap; text-align: left;
}
.bulk-table td { padding: 4px 6px; border-bottom: 1px solid var(--border); }
.bulk-table tr:last-child td { border-bottom: none; }
.bulk-table tr:hover td { background: rgba(255,255,255,0.02); }
.bulk-venue { font-weight: 800; color: var(--text); padding-left: 10px; white-space: nowrap; font-size: 13px; }
.bulk-input {
  width: 76px; background: var(--bg-input); border: 1px solid var(--border2);
  border-radius: 6px; padding: 5px 7px; color: var(--text);
  font-size: 12px; font-family: var(--font-mono); outline: none; transition: border-color 0.12s;
}
.bulk-input:focus { border-color: var(--accent); }
.bulk-select {
  background: var(--bg-input); border: 1px solid var(--border2);
  border-radius: 6px; padding: 4px 5px; color: var(--text);
  font-size: 11px; outline: none; width: 64px;
}

/* ── Warn badge ── */
.warn-inline {
  font-size: 10px; color: var(--red); font-weight: 700; margin-left: 4px;
}

/* ── Toast ── */
.toast-container { position: fixed; bottom: 20px; right: 20px; z-index: 999; display: flex; flex-direction: column; gap: 8px; }
.toast {
  background: var(--bg-card2); border: 1px solid var(--border2);
  border-radius: 10px; padding: 12px 16px; font-size: 13px; font-weight: 600;
  color: var(--text); box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  animation: slideIn 0.2s ease; min-width: 240px;
}
.toast.success { border-color: var(--green); color: var(--green); }
.toast.error   { border-color: var(--red);   color: var(--red);   }
@keyframes slideIn { from { transform: translateX(20px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
</style>
</head>
<body>

<nav class="nav">
  <div class="nav-title">
    <div class="icon">🏇</div>
    管理ダッシュボード
    <span class="nav-badge">keiba-scatter-v2</span>
  </div>
  <div class="nav-actions">
    <a id="pages-link" href="#" target="_blank" class="nav-link">GitHub Pages ↗</a>
    <a id="repo-link"  href="#" target="_blank" class="nav-link">Repository ↗</a>
  </div>
</nav>

<div class="container">

  <!-- ── Status ── -->
  <div class="status-row">
    <div class="stat-card">
      <div class="stat-label">クッションDB</div>
      <div class="stat-value green" id="db-count">—</div>
      <div class="stat-sub">レコード数</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">観測データ</div>
      <div class="stat-value purple" id="obs-count">—</div>
      <div class="stat-sub">件数</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">出力済み日付</div>
      <div class="stat-value amber" id="output-count">—</div>
      <div class="stat-sub" id="latest-date">最新: —</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">デプロイ先</div>
      <div class="stat-value blue" style="font-size:13px;margin-top:8px" id="repo-name">—</div>
      <div class="stat-sub" id="pages-url-text">—</div>
    </div>
  </div>

  <!-- ── Tab navigation ── -->
  <div class="tab-nav">
    <button class="tab-btn active" id="tab-pipeline" onclick="switchTab('pipeline')">▶ パイプライン</button>
    <button class="tab-btn"        id="tab-obs"      onclick="switchTab('obs')">📊 観測データ</button>
    <button class="tab-btn"        id="tab-bulk"     onclick="switchTab('bulk')">📋 一括入力</button>
  </div>

  <!-- ══════════════════════════════════════
       Tab: Pipeline
       ══════════════════════════════════════ -->
  <div id="section-pipeline">
    <div class="grid">

      <!-- Left panel -->
      <div style="display:flex;flex-direction:column;gap:16px;">

        <!-- Pipeline form -->
        <div class="card">
          <div class="card-header">
            <h2><span>▶</span> パイプライン実行</h2>
          </div>
          <div class="card-body">
            <div class="form-group">
              <label class="form-label">開催日 (YYYYMMDD)</label>
              <input type="text" id="date-input" class="form-input" placeholder="例: 20260419" maxlength="8">
            </div>
            <div class="form-row">
              <div class="form-group">
                <label class="form-label">会場フィルター</label>
                <input type="text" id="venue-input" class="form-input" placeholder="東京 / 京都 …">
              </div>
              <div class="form-group">
                <label class="form-label">R番号フィルター</label>
                <input type="number" id="race-input" class="form-input" placeholder="11" min="1" max="12">
              </div>
            </div>
            <div class="form-group">
              <label class="form-label">オプション</label>
              <div class="checkbox-group">
                <label class="checkbox-item" id="cb-deploy">
                  <input type="checkbox" id="opt-deploy">
                  <span class="cb-box"><span class="cb-check">✓</span></span>
                  <span class="cb-label">GitHub Pagesへデプロイ</span>
                  <span class="cb-desc">--deploy</span>
                </label>
                <label class="checkbox-item" id="cb-no-scrape">
                  <input type="checkbox" id="opt-no-scrape">
                  <span class="cb-box"><span class="cb-check">✓</span></span>
                  <span class="cb-label">キャッシュ使用（再スクレイプしない）</span>
                  <span class="cb-desc">--no-scrape</span>
                </label>
                <label class="checkbox-item" id="cb-manual">
                  <input type="checkbox" id="opt-manual">
                  <span class="cb-box"><span class="cb-check">✓</span></span>
                  <span class="cb-label">CV・含水率を手動入力</span>
                  <span class="cb-desc">--manual</span>
                </label>
                <label class="checkbox-item" id="cb-force-update">
                  <input type="checkbox" id="opt-force-update">
                  <span class="cb-box"><span class="cb-check">✓</span></span>
                  <span class="cb-label">DBデータを強制上書き</span>
                  <span class="cb-desc">--force-update</span>
                </label>
                <label class="checkbox-item" id="cb-cleanup">
                  <input type="checkbox" id="opt-cleanup">
                  <span class="cb-box"><span class="cb-check">✓</span></span>
                  <span class="cb-label">旧ファイルをGitHubから削除</span>
                  <span class="cb-desc">--cleanup</span>
                </label>
              </div>
            </div>
            <div class="btn-group">
              <button class="btn btn-primary btn-full" id="run-btn" onclick="runPipeline()">▶ 実行</button>
              <button class="btn btn-danger" id="stop-btn" onclick="stopJob()" style="display:none">■ 停止</button>
            </div>
          </div>
        </div>

        <!-- Quick actions -->
        <div class="card">
          <div class="card-header"><h2><span>⚡</span> クイックアクション</h2></div>
          <div class="card-body">
            <div class="quick-grid">
              <button class="quick-btn" onclick="weekendScrape()" style="border-color:rgba(225,29,72,0.5);background:rgba(225,29,72,0.1)">
                <div class="qb-icon">📥</div>
                <div class="qb-label">今週末を一括取得</div>
                <div class="qb-desc">土日フルスクレイピング（木曜夜）</div>
              </button>
              <button class="quick-btn" onclick="updateDb()">
                <div class="qb-icon">🗄</div>
                <div class="qb-label">DB更新</div>
                <div class="qb-desc">クッション値DBを最新化</div>
              </button>
              <button class="quick-btn" onclick="deployOnly()">
                <div class="qb-icon">🚀</div>
                <div class="qb-label">デプロイのみ</div>
                <div class="qb-desc">生成済みHTMLをアップロード</div>
              </button>
              <button class="quick-btn" onclick="openPages()">
                <div class="qb-icon">🌐</div>
                <div class="qb-label">サイトを開く</div>
                <div class="qb-desc">GitHub Pages を表示</div>
              </button>
              <button class="quick-btn" onclick="refreshStatus()">
                <div class="qb-icon">↻</div>
                <div class="qb-label">ステータス更新</div>
                <div class="qb-desc">情報を再取得</div>
              </button>
            </div>
          </div>
        </div>

        <!-- History -->
        <div class="card">
          <div class="card-header"><h2><span>📁</span> 出力履歴</h2></div>
          <div class="card-body">
            <div class="history-list" id="history-list">
              <div style="color:var(--text-muted);font-size:12px">読み込み中...</div>
            </div>
          </div>
        </div>

      </div><!-- /left panel -->

      <!-- Terminal -->
      <div class="terminal-wrap">
        <div class="terminal-header">
          <div class="terminal-dots">
            <span class="td-red"></span><span class="td-yellow"></span><span class="td-green"></span>
          </div>
          <div class="terminal-title" id="terminal-title">ログ出力</div>
          <div class="terminal-status">
            <div class="status-dot" id="status-dot">
              <span class="dot"></span>
              <span id="status-text">待機中</span>
            </div>
          </div>
        </div>
        <div class="terminal-body" id="terminal">
          <div class="log-line sep">━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</div>
          <div class="log-line" style="color:var(--accent)">  🏇  keiba-scatter-v2 管理ダッシュボード</div>
          <div class="log-line sep">━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</div>
          <div class="log-line" style="color:var(--text-muted)">左のフォームで日付・オプションを設定して [▶ 実行] を押してください。</div>
        </div>
        <div class="terminal-footer">
          <span class="log-count" id="log-count">0 行</span>
          <button class="btn-clear" onclick="clearLog()">クリア</button>
        </div>
      </div>

    </div><!-- /grid -->
  </div><!-- /section-pipeline -->

  <!-- ══════════════════════════════════════
       Tab: 観測データ
       ══════════════════════════════════════ -->
  <div id="section-obs" style="display:none">
    <div class="obs-grid">

      <!-- 入力フォーム -->
      <div style="display:flex;flex-direction:column;gap:16px;">
        <div class="card">
          <div class="card-header">
            <h2><span>📊</span> 観測データ入力</h2>
            <span class="nav-badge" id="obs-edit-badge">新規</span>
          </div>
          <div class="card-body">

            <!-- Edit mode bar -->
            <div class="edit-mode-bar" id="obs-edit-bar" style="display:none">
              <span>✏️ 編集中: <span id="obs-edit-id"></span></span>
              <button class="btn-sm" onclick="cancelEditObs()">キャンセル</button>
            </div>

            <!-- 基本情報 -->
            <div class="section-title">基本情報</div>
            <div class="form-row">
              <div class="form-group">
                <label class="form-label">日付 *</label>
                <input type="date" id="obs-date" class="form-input">
              </div>
              <div class="form-group">
                <label class="form-label">会場 *</label>
                <select id="obs-venue" class="form-input">
                  <option value="">選択...</option>
                  <option value="nakayama">中山</option>
                  <option value="tokyo">東京</option>
                  <option value="hanshin">阪神</option>
                  <option value="kyoto">京都</option>
                  <option value="chukyo">中京</option>
                  <option value="fukushima">福島</option>
                  <option value="niigata">新潟</option>
                  <option value="kokura">小倉</option>
                  <option value="sapporo">札幌</option>
                  <option value="hakodate">函館</option>
                </select>
              </div>
            </div>
            <div class="form-group">
              <label class="form-label">路面 *</label>
              <div class="surface-group">
                <button class="surf-btn active-turf" id="surf-turf" onclick="selectSurface('turf')">芝 (Turf)</button>
                <button class="surf-btn" id="surf-dirt" onclick="selectSurface('dirt')">ダート (Dirt)</button>
              </div>
              <input type="hidden" id="obs-surface" value="turf">
            </div>

            <!-- CV・含水率 -->
            <div class="section-title">CV・含水率</div>
            <div class="form-row">
              <div class="form-group">
                <label class="form-label">クッション値 <span id="cv-warn" class="warn-inline" style="display:none">⚠ 要確認</span></label>
                <input type="number" id="obs-cv" class="form-input" placeholder="例: 9.6" step="0.1" min="0" max="30" oninput="checkCvWarn()">
              </div>
              <div class="form-group">
                <label class="form-label">含水率 (%)</label>
                <input type="number" id="obs-moisture" class="form-input" placeholder="例: 9.8" step="0.1" min="0" max="100">
              </div>
            </div>
            <div class="form-group">
              <label class="form-label">測定時刻</label>
              <input type="time" id="obs-time" class="form-input">
            </div>

            <!-- 気象条件 -->
            <details class="collapsible">
              <summary>🌤 気象条件</summary>
              <div class="detail-body">
                <div class="form-row">
                  <div class="form-group">
                    <label class="form-label">天候</label>
                    <select id="obs-weather" class="form-input">
                      <option value="">—</option>
                      <option value="sunny">晴</option>
                      <option value="cloudy">曇</option>
                      <option value="rain">雨</option>
                      <option value="mixed">混合</option>
                    </select>
                  </div>
                  <div class="form-group">
                    <label class="form-label">馬場状態</label>
                    <select id="obs-track" class="form-input">
                      <option value="">—</option>
                      <option value="良">良</option>
                      <option value="稍重">稍重</option>
                      <option value="重">重</option>
                      <option value="不良">不良</option>
                    </select>
                  </div>
                </div>
                <div class="form-row">
                  <div class="form-group">
                    <label class="form-label">平均気温 (℃)</label>
                    <input type="number" id="obs-temp" class="form-input" placeholder="例: 18.5" step="0.1">
                  </div>
                  <div class="form-group">
                    <label class="form-label">平均湿度 (%)</label>
                    <input type="number" id="obs-humidity" class="form-input" placeholder="例: 60" step="1">
                  </div>
                </div>
                <div class="form-row">
                  <div class="form-group">
                    <label class="form-label">平均風速 (m/s)</label>
                    <input type="number" id="obs-wind" class="form-input" placeholder="例: 3.2" step="0.1">
                  </div>
                  <div class="form-group">
                    <label class="form-label">前24h降水 (mm)</label>
                    <input type="number" id="obs-rain24" class="form-input" placeholder="例: 0" step="0.1">
                  </div>
                </div>
                <div class="form-group">
                  <label class="form-label">前週累積降水 (mm)</label>
                  <input type="number" id="obs-rainweek" class="form-input" placeholder="例: 12.5" step="0.1">
                </div>
              </div>
            </details>

            <!-- 馬場作業 -->
            <details class="collapsible">
              <summary>🚜 馬場作業</summary>
              <div class="detail-body">
                <div class="form-group">
                  <label class="form-label">散水</label>
                  <label class="checkbox-item" id="cb-watering">
                    <input type="checkbox" id="obs-watering">
                    <span class="cb-box"><span class="cb-check">✓</span></span>
                    <span class="cb-label">散水あり</span>
                  </label>
                </div>
                <div class="form-group">
                  <label class="form-label">散水メモ</label>
                  <input type="text" id="obs-watering-notes" class="form-input" placeholder="例: 前日18時に散水">
                </div>
                <div class="form-group">
                  <label class="form-label">作業履歴</label>
                  <input type="text" id="obs-maint" class="form-input" placeholder="例: 芝刈り、水分エアレーション">
                </div>
              </div>
            </details>

            <!-- その他 -->
            <details class="collapsible">
              <summary>📎 その他</summary>
              <div class="detail-body">
                <div class="form-group">
                  <label class="form-label">参照URL</label>
                  <input type="text" id="obs-url" class="form-input" placeholder="JRA馬場情報ページなど">
                </div>
                <div class="form-group">
                  <label class="form-label">メモ</label>
                  <input type="text" id="obs-notes" class="form-input" placeholder="自由記述">
                </div>
              </div>
            </details>

            <div class="btn-group">
              <button class="btn btn-primary btn-full" id="obs-save-btn" onclick="saveObs()">💾 保存</button>
              <button class="btn btn-secondary" onclick="clearObsForm()">クリア</button>
            </div>
          </div>
        </div>
      </div><!-- /form column -->

      <!-- データ一覧 -->
      <div style="display:flex;flex-direction:column;gap:16px;">
        <div class="card" style="flex:1">
          <div class="card-header">
            <h2><span>📋</span> 観測データ一覧 <span class="nav-badge" id="obs-list-count">0件</span></h2>
            <div style="display:flex;gap:6px">
              <button class="btn btn-secondary" style="padding:6px 12px;font-size:11px" onclick="loadObsList()">↻ 更新</button>
              <button class="btn btn-green"     style="padding:6px 12px;font-size:11px" onclick="exportObs()">⬇ CSV</button>
            </div>
          </div>
          <div class="card-body" style="padding:12px">
            <div class="obs-list" id="obs-list">
              <div style="color:var(--text-muted);font-size:12px;padding:8px">読み込み中...</div>
            </div>
          </div>
        </div>
      </div><!-- /list column -->

    </div><!-- /obs-grid -->
  </div><!-- /section-obs -->

  <!-- ══════════════════════════════════════
       Tab: 一括入力
       ══════════════════════════════════════ -->
  <div id="section-bulk" style="display:none">
    <div style="display:flex;flex-direction:column;gap:16px">

      <!-- JRA自動取得 -->
      <div class="card">
        <div class="card-header">
          <h2><span>🤖</span> JRAから自動取得</h2>
        </div>
        <div class="card-body">
          <div style="font-size:12px;color:var(--text-sub);margin-bottom:14px;line-height:1.7">
            JRA公式の馬場情報ページから<b>クッション値・含水率</b>を自動スクレイピングして<br>
            observations.csv に保存します。<br>
            <span style="color:var(--text-muted);font-size:11px">※ 取得できるのは現在JRAが公開中の日程のみです。</span>
          </div>
          <div class="form-row" style="margin-bottom:14px">
            <div class="form-group" style="margin-bottom:0">
              <label class="form-label">日付を絞り込み（空欄=全日程）</label>
              <input type="date" id="fetch-date" class="form-input">
            </div>
            <div class="form-group" style="margin-bottom:0;display:flex;align-items:flex-end">
              <button class="btn btn-primary btn-full" id="fetch-btn" onclick="fetchFromJRA()">
                🤖 JRAからデータ取得
              </button>
            </div>
          </div>
          <div id="fetch-result" style="display:none;padding:10px 14px;border-radius:8px;font-size:13px;font-weight:700"></div>
        </div>
      </div>

      <!-- 気象データ紐付け -->
      <div class="card">
        <div class="card-header">
          <h2><span>🌤</span> 気象データ紐付け（気象庁）</h2>
        </div>
        <div class="card-body">
          <div style="font-size:12px;color:var(--text-sub);margin-bottom:14px;line-height:1.7">
            気象庁の観測データから各会場の<b>気温・湿度・風速・天気・降水量</b>を自動取得して紐付けます。<br>
            未入力のレコードのみ対象（約3〜5分かかります）。
          </div>
          <div class="form-row" style="margin-bottom:14px">
            <div class="form-group" style="margin-bottom:0;display:flex;align-items:flex-end">
              <button class="btn btn-primary btn-full" id="weather-btn" onclick="fetchWeather()">
                🌤 気象データを取得・紐付け
              </button>
            </div>
          </div>
          <div id="weather-result" style="display:none;padding:10px 14px;border-radius:8px;font-size:13px;font-weight:700"></div>
        </div>
      </div>

      <!-- JRA PDFアーカイブ取得 -->
      <div class="card">
        <div class="card-header">
          <h2><span>📥</span> JRA過去データ取得（PDFアーカイブ）</h2>
        </div>
        <div class="card-body">
          <div style="font-size:12px;color:var(--text-sub);margin-bottom:14px;line-height:1.7">
            JRA公式の<b>PDFアーカイブ</b>から過去のクッション値・含水率を一括取得します。<br>
            年度を指定してください（1年分で数分かかります）。
          </div>
          <div class="form-row" style="margin-bottom:14px">
            <div class="form-group" style="margin-bottom:0">
              <label class="form-label">取得開始年</label>
              <input type="number" id="pdf-year-from" class="form-input" min="2020" max="2030" value="2025" style="width:100px">
            </div>
            <div class="form-group" style="margin-bottom:0">
              <label class="form-label">取得終了年</label>
              <input type="number" id="pdf-year-to" class="form-input" min="2020" max="2030" value="2026" style="width:100px">
            </div>
            <div class="form-group" style="margin-bottom:0;display:flex;align-items:flex-end">
              <button class="btn btn-primary btn-full" id="pdf-btn" onclick="fetchFromJRAPdf()">
                📥 PDFから過去データ取得
              </button>
            </div>
          </div>
          <div id="pdf-result" style="display:none;padding:10px 14px;border-radius:8px;font-size:13px;font-weight:700"></div>
        </div>
      </div>

      <!-- 日付別一括入力 -->
      <div class="card">
        <div class="card-header">
          <h2><span>📋</span> 日付別一括入力</h2>
          <button class="btn btn-primary" style="padding:7px 14px;font-size:12px" onclick="saveBulk()">💾 一括保存</button>
        </div>
        <div class="card-body">
          <div class="form-row-3" style="margin-bottom:16px">
            <div class="form-group" style="margin-bottom:0">
              <label class="form-label">日付 *</label>
              <input type="date" id="bulk-date" class="form-input">
            </div>
            <div class="form-group" style="margin-bottom:0">
              <label class="form-label">測定時刻</label>
              <input type="time" id="bulk-time" class="form-input">
            </div>
            <div class="form-group" style="margin-bottom:0">
              <label class="form-label">天候（共通）</label>
              <select id="bulk-weather" class="form-input">
                <option value="">—</option>
                <option value="sunny">晴</option>
                <option value="cloudy">曇</option>
                <option value="rain">雨</option>
                <option value="mixed">混合</option>
              </select>
            </div>
          </div>

          <div style="overflow-x:auto">
            <table class="bulk-table">
              <thead>
                <tr>
                  <th>会場</th>
                  <th style="color:#4ade80">芝 CV</th>
                  <th style="color:#4ade80">芝 含水率%</th>
                  <th style="color:#4ade80">芝 馬場</th>
                  <th style="color:#fbbf24">ダ 含水率%</th>
                  <th style="color:#fbbf24">ダ 馬場</th>
                </tr>
              </thead>
              <tbody id="bulk-tbody"></tbody>
            </table>
          </div>
          <div style="margin-top:10px;font-size:11px;color:var(--text-muted)">
            ※ 入力した会場のみ保存。芝CVはダートには適用されません。既存ID（同日同会場同路面）はスキップ。
          </div>
        </div>
      </div>

      <!-- CSVインポート -->
      <div class="card">
        <div class="card-header">
          <h2><span>⬆</span> CSVインポート（過去データ一括）</h2>
        </div>
        <div class="card-body">
          <div class="form-group">
            <label class="form-label">CSVファイルを選択</label>
            <input type="file" id="import-file" accept=".csv" class="form-input" style="cursor:pointer;padding:7px">
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:12px">
            observations.csv と同じ列構成のCSVを読み込みます。テンプレートを参考に作成してください。<br>
            既存の同一ID（日付＋会場＋路面）はスキップされます。
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-green"     onclick="importCsv()">⬆ インポート実行</button>
            <button class="btn btn-secondary" onclick="window.location.href='/api/obs/template'">⬇ テンプレートCSV</button>
          </div>
          <div id="import-result" style="margin-top:12px;font-size:12px;font-weight:700;display:none"></div>
        </div>
      </div>

    </div>
  </div><!-- /section-bulk -->

</div><!-- /container -->

<div class="toast-container" id="toast-container"></div>

<script>
let evtSource = null;
let logCount  = 0;
let pagesUrl  = '';
let editingObsId = null;
let currentSurface = 'turf';

const VENUES = [
  {key:'nakayama', ja:'中山'}, {key:'tokyo',    ja:'東京'},
  {key:'hanshin',  ja:'阪神'}, {key:'kyoto',    ja:'京都'},
  {key:'chukyo',   ja:'中京'}, {key:'fukushima',ja:'福島'},
  {key:'niigata',  ja:'新潟'}, {key:'kokura',   ja:'小倉'},
  {key:'sapporo',  ja:'札幌'}, {key:'hakodate', ja:'函館'},
];

// ════════════════ Init ════════════════

async function init() {
  setTodayDate();
  setObsDate();
  initBulkTable();
  setBulkDate();
  await refreshStatus();
  initCheckboxes();
  loadObsList();
}

function setTodayDate() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth()+1).padStart(2,'0');
  const d = String(now.getDate()).padStart(2,'0');
  document.getElementById('date-input').value = `${y}${m}${d}`;
}

function setObsDate() {
  const now = new Date();
  document.getElementById('obs-date').value = now.toISOString().slice(0,10);
}

// ════════════════ Tab ════════════════

function switchTab(tab) {
  ['pipeline','obs','bulk'].forEach(t => {
    document.getElementById(`section-${t}`).style.display = t === tab ? '' : 'none';
    document.getElementById(`tab-${t}`).classList.toggle('active', t === tab);
  });
  if (tab === 'obs')  loadObsList();
  if (tab === 'bulk') setBulkDate();
}

// ════════════════ Bulk Entry ════════════════

function setBulkDate() {
  const el = document.getElementById('bulk-date');
  if (!el.value) el.value = new Date().toISOString().slice(0, 10);
}

function initBulkTable() {
  const trackOpts = '<option value="">—</option><option value="良">良</option><option value="稍重">稍重</option><option value="重">重</option><option value="不良">不良</option>';
  document.getElementById('bulk-tbody').innerHTML = VENUES.map(v => `
    <tr>
      <td class="bulk-venue">${v.ja}</td>
      <td><input type="number" step="0.1" min="0" max="30"  class="bulk-input bulk-cv"  data-v="${v.key}" placeholder="9.6"></td>
      <td><input type="number" step="0.1" min="0" max="100" class="bulk-input bulk-mot" data-v="${v.key}" placeholder="9.8"></td>
      <td><select class="bulk-select bulk-tkt" data-v="${v.key}">${trackOpts}</select></td>
      <td><input type="number" step="0.1" min="0" max="100" class="bulk-input bulk-mod" data-v="${v.key}" placeholder="9.8"></td>
      <td><select class="bulk-select bulk-tkd" data-v="${v.key}">${trackOpts}</select></td>
    </tr>`).join('');
}

async function saveBulk() {
  const date    = document.getElementById('bulk-date').value;
  const time    = document.getElementById('bulk-time').value;
  const weather = document.getElementById('bulk-weather').value;
  if (!date) { toast('日付を入力してください', 'error'); return; }

  const items = [];
  VENUES.forEach(v => {
    const cv  = document.querySelector(`.bulk-cv[data-v="${v.key}"]`).value;
    const mot = document.querySelector(`.bulk-mot[data-v="${v.key}"]`).value;
    const tkt = document.querySelector(`.bulk-tkt[data-v="${v.key}"]`).value;
    const mod = document.querySelector(`.bulk-mod[data-v="${v.key}"]`).value;
    const tkd = document.querySelector(`.bulk-tkd[data-v="${v.key}"]`).value;
    if (cv || mot || tkt)
      items.push({date, venue:v.key, surface:'turf', cushion_value:cv, moisture_rate:mot,
                  measurement_time:time, weather, track_condition:tkt});
    if (mod || tkd)
      items.push({date, venue:v.key, surface:'dirt', cushion_value:'', moisture_rate:mod,
                  measurement_time:time, weather, track_condition:tkd});
  });
  if (!items.length) { toast('入力データがありません', 'error'); return; }

  const r   = await fetch('/api/obs/bulk', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(items)});
  const res = await r.json();
  if (res.error) { toast(res.error, 'error'); return; }
  toast(`保存 ${res.created}件 / スキップ(重複) ${res.skipped}件`, 'success');
  refreshStatus();
}

async function importCsv() {
  const file = document.getElementById('import-file').files[0];
  if (!file) { toast('CSVファイルを選択してください', 'error'); return; }
  const form = new FormData();
  form.append('file', file);
  const r   = await fetch('/api/obs/import', {method:'POST', body:form});
  const res = await r.json();
  const el  = document.getElementById('import-result');
  el.style.display = '';
  if (res.error) { el.style.color='var(--red)'; el.textContent=`エラー: ${res.error}`; return; }
  el.style.color='var(--green)';
  el.textContent = `完了 — 新規: ${res.created}件 / スキップ(重複): ${res.skipped}件 / エラー行: ${res.errors}件`;
  toast('インポート完了 ✓', 'success');
  refreshStatus();
}

// ════════════════ Status ════════════════

async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('db-count').textContent     = s.db_count.toLocaleString();
    document.getElementById('obs-count').textContent    = s.obs_count.toLocaleString();
    document.getElementById('output-count').textContent = s.output_dates.length;
    document.getElementById('latest-date').textContent  = s.output_dates.length > 0 ? `最新: ${s.output_dates[0]}` : '最新: —';
    document.getElementById('repo-name').textContent    = s.repo || '未設定';
    document.getElementById('pages-url-text').textContent = s.pages_url || '—';
    pagesUrl = s.pages_url || '';
    if (s.pages_url) {
      document.getElementById('pages-link').href = s.pages_url;
      document.getElementById('repo-link').href  = `https://github.com/${s.repo}`;
    }
    renderHistory(s.output_dates);
    if (s.running) setRunningState(true);
  } catch(e) { console.error(e); }
}

function renderHistory(dates) {
  const el = document.getElementById('history-list');
  if (!dates.length) {
    el.innerHTML = '<div style="color:var(--text-muted);font-size:12px">出力済みデータなし</div>';
    return;
  }
  el.innerHTML = dates.map(d => {
    const fmt = `${d.slice(0,4)}/${d.slice(4,6)}/${d.slice(6,8)}`;
    return `<div class="history-item">
      <div>
        <div class="history-date">${fmt}</div>
        <div class="history-meta">${d}</div>
      </div>
      <div class="history-actions">
        <button class="btn-sm" onclick="setDate('${d}')">選択</button>
        <button class="btn-sm" onclick="deployDate('${d}')">デプロイ</button>
      </div>
    </div>`;
  }).join('');
}

function setDate(d) {
  document.getElementById('date-input').value = d;
  toast(`日付を ${d} に設定しました`);
}

function deployDate(d) {
  document.getElementById('date-input').value = d;
  document.getElementById('opt-deploy').checked    = true;
  document.getElementById('opt-no-scrape').checked = true;
  document.getElementById('cb-deploy').classList.add('checked');
  document.getElementById('cb-no-scrape').classList.add('checked');
  runPipeline();
}

// ════════════════ Checkboxes ════════════════

function initCheckboxes() {
  document.querySelectorAll('.checkbox-item').forEach(item => {
    const cb = item.querySelector('input[type=checkbox]');
    if (!cb) return;
    item.addEventListener('click', () => {
      cb.checked = !cb.checked;
      item.classList.toggle('checked', cb.checked);
    });
  });
}

function getOptions() {
  return {
    date:         document.getElementById('date-input').value.trim(),
    venue:        document.getElementById('venue-input').value.trim(),
    race:         document.getElementById('race-input').value.trim(),
    deploy:       document.getElementById('opt-deploy').checked,
    no_scrape:    document.getElementById('opt-no-scrape').checked,
    manual:       document.getElementById('opt-manual').checked,
    force_update: document.getElementById('opt-force-update').checked,
    cleanup:      document.getElementById('opt-cleanup').checked,
  };
}

// ════════════════ Pipeline ════════════════

async function runPipeline() {
  const opts = getOptions();
  if (!opts.date || !/^\d{8}$/.test(opts.date)) {
    toast('日付を YYYYMMDD 形式で入力してください', 'error'); return;
  }
  const r = await fetch('/api/run', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(opts),
  });
  const data = await r.json();
  if (data.error) { toast(data.error, 'error'); return; }
  startStream(`$ ${data.cmd}`);
}

async function weekendScrape() {
  if (!confirm('今週末（土・日）のフルスクレイピングを実行します。\\n時間がかかります。よろしいですか？')) return;
  const r = await fetch('/api/weekend-scrape', { method: 'POST' });
  const data = await r.json();
  if (data.error) { toast(data.error, 'error'); return; }
  startStream('$ python pipeline.py 土曜 --deploy && python pipeline.py 日曜 --deploy');
}

async function updateDb() {
  const r = await fetch('/api/update-db', { method: 'POST' });
  const data = await r.json();
  if (data.error) { toast(data.error, 'error'); return; }
  startStream('$ python update_cushion_db.py');
}

async function deployOnly() {
  const date    = document.getElementById('date-input').value.trim();
  const cleanup = document.getElementById('opt-cleanup').checked;
  if (!date || !/^\d{8}$/.test(date)) { toast('日付を選択してください', 'error'); return; }
  const r = await fetch('/api/deploy-only', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ date, cleanup }),
  });
  const data = await r.json();
  if (data.error) { toast(data.error, 'error'); return; }
  startStream(`$ ${data.cmd}`);
}

async function stopJob() {
  await fetch('/api/stop', { method: 'POST' });
  toast('停止しました');
}

function openPages() {
  if (pagesUrl) window.open(pagesUrl, '_blank');
  else toast('GitHub Pages URLが設定されていません', 'error');
}

// ════════════════ SSE Stream ════════════════

function startStream(cmdLabel) {
  if (evtSource) { evtSource.close(); evtSource = null; }
  clearLog();
  setRunningState(true);
  appendLog(cmdLabel, 'step');
  appendLog('━'.repeat(60), 'sep');

  evtSource = new EventSource('/stream');
  evtSource.onmessage = (e) => {
    const line = JSON.parse(e.data);
    if (line === '__PING__') return;
    if (line.startsWith('__EXIT__')) {
      const code = parseInt(line.replace('__EXIT__', ''));
      evtSource.close(); evtSource = null;
      setRunningState(false);
      appendLog('━'.repeat(60), 'sep');
      if      (code === 0)  { appendLog('✓ 完了 (exit 0)', 'exit-ok'); toast('実行完了！', 'success'); refreshStatus(); }
      else if (code === -1) { appendLog('■ 停止しました', 'warn'); }
      else                  { appendLog(`✗ エラー終了 (exit ${code})`, 'exit-ng'); toast(`エラーが発生しました (${code})`, 'error'); }
      return;
    }
    appendLog(line);
  };
  evtSource.onerror = () => {
    setRunningState(false);
    if (evtSource) { evtSource.close(); evtSource = null; }
  };
}

function appendLog(text, cls = '') {
  const el   = document.getElementById('terminal');
  const line = document.createElement('div');
  if (!cls) {
    if      (text.startsWith('  OK ') || text.includes('✓') || text.includes('完了')) cls = 'ok';
    else if (text.startsWith('  NG ') || text.includes('✗') || text.includes('ERROR')) cls = 'ng';
    else if (text.startsWith('===') || text.startsWith('[Step') || text.startsWith('[Deploy')) cls = 'step';
    else if (text.startsWith('---')) cls = 'sep';
    else if (text.includes('デプロイ') || text.includes('GitHub')) cls = 'deploy';
    else if (text.includes('WARNING') || text.includes('警告') || text.includes('スキップ')) cls = 'warn';
  }
  line.className   = 'log-line ' + cls;
  line.textContent = text;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
  logCount++;
  document.getElementById('log-count').textContent = `${logCount} 行`;
}

function clearLog() {
  document.getElementById('terminal').innerHTML = '';
  logCount = 0;
  document.getElementById('log-count').textContent = '0 行';
}

function setRunningState(running) {
  const runBtn    = document.getElementById('run-btn');
  const stopBtn   = document.getElementById('stop-btn');
  const dot       = document.getElementById('status-dot');
  const statusTxt = document.getElementById('status-text');
  const title     = document.getElementById('terminal-title');
  runBtn.disabled = running;
  stopBtn.style.display = running ? 'inline-flex' : 'none';
  if (running) {
    dot.className = 'status-dot running'; statusTxt.textContent = '実行中'; title.textContent = '実行中...';
  } else {
    dot.className = 'status-dot'; statusTxt.textContent = '待機中'; title.textContent = 'ログ出力';
  }
}

// ════════════════ Observations ════════════════

function selectSurface(surf) {
  currentSurface = surf;
  document.getElementById('obs-surface').value = surf;
  document.getElementById('surf-turf').className = 'surf-btn' + (surf === 'turf' ? ' active-turf' : '');
  document.getElementById('surf-dirt').className = 'surf-btn' + (surf === 'dirt' ? ' active-dirt' : '');
}

function checkCvWarn() {
  const v    = parseFloat(document.getElementById('obs-cv').value);
  const warn = document.getElementById('cv-warn');
  warn.style.display = (!isNaN(v) && (v < 3 || v > 20)) ? '' : 'none';
}

function getObsFormData() {
  const watering = document.getElementById('obs-watering').checked;
  return {
    date:             document.getElementById('obs-date').value,
    venue:            document.getElementById('obs-venue').value,
    surface:          document.getElementById('obs-surface').value,
    cushion_value:    document.getElementById('obs-cv').value,
    moisture_rate:    document.getElementById('obs-moisture').value,
    measurement_time: document.getElementById('obs-time').value,
    weather:          document.getElementById('obs-weather').value,
    track_condition:  document.getElementById('obs-track').value,
    temperature_avg:  document.getElementById('obs-temp').value,
    humidity_avg:     document.getElementById('obs-humidity').value,
    wind_speed_avg:   document.getElementById('obs-wind').value,
    rainfall_24h:     document.getElementById('obs-rain24').value,
    rainfall_prev_week: document.getElementById('obs-rainweek').value,
    watering:         watering ? '1' : '0',
    watering_notes:   document.getElementById('obs-watering-notes').value,
    maintenance_notes: document.getElementById('obs-maint').value,
    source_url:       document.getElementById('obs-url').value,
    notes:            document.getElementById('obs-notes').value,
  };
}

function clearObsForm() {
  editingObsId = null;
  document.getElementById('obs-edit-bar').style.display = 'none';
  document.getElementById('obs-edit-badge').textContent = '新規';
  document.getElementById('obs-save-btn').textContent   = '💾 保存';
  setObsDate();
  document.getElementById('obs-venue').value = '';
  selectSurface('turf');
  ['obs-cv','obs-moisture','obs-time','obs-weather','obs-track',
   'obs-temp','obs-humidity','obs-wind','obs-rain24','obs-rainweek',
   'obs-watering-notes','obs-maint','obs-url','obs-notes'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = el.tagName === 'SELECT' ? '' : '';
  });
  document.getElementById('obs-watering').checked = false;
  document.getElementById('cb-watering').classList.remove('checked');
  document.getElementById('cv-warn').style.display = 'none';
}

function loadObsIntoForm(row) {
  editingObsId = row.id;
  document.getElementById('obs-edit-bar').style.display = '';
  document.getElementById('obs-edit-id').textContent    = row.id;
  document.getElementById('obs-edit-badge').textContent = '編集中';
  document.getElementById('obs-save-btn').textContent   = '💾 更新';

  document.getElementById('obs-date').value    = row.date || '';
  document.getElementById('obs-venue').value   = row.venue || '';
  selectSurface(row.surface || 'turf');
  document.getElementById('obs-cv').value       = row.cushion_value || '';
  document.getElementById('obs-moisture').value = row.moisture_rate || '';
  document.getElementById('obs-time').value     = row.measurement_time || '';
  document.getElementById('obs-weather').value  = row.weather || '';
  document.getElementById('obs-track').value    = row.track_condition || '';
  document.getElementById('obs-temp').value     = row.temperature_avg || '';
  document.getElementById('obs-humidity').value = row.humidity_avg || '';
  document.getElementById('obs-wind').value     = row.wind_speed_avg || '';
  document.getElementById('obs-rain24').value   = row.rainfall_24h || '';
  document.getElementById('obs-rainweek').value = row.rainfall_prev_week || '';
  const water = row.watering === '1';
  document.getElementById('obs-watering').checked = water;
  document.getElementById('cb-watering').classList.toggle('checked', water);
  document.getElementById('obs-watering-notes').value = row.watering_notes || '';
  document.getElementById('obs-maint').value    = row.maintenance_notes || '';
  document.getElementById('obs-url').value      = row.source_url || '';
  document.getElementById('obs-notes').value    = row.notes || '';
  checkCvWarn();
}

function cancelEditObs() { clearObsForm(); }

async function saveObs() {
  const data = getObsFormData();
  if (!data.date || !data.venue) {
    toast('日付と会場は必須です', 'error'); return;
  }

  let r;
  if (editingObsId) {
    r = await fetch(`/api/obs/${encodeURIComponent(editingObsId)}`, {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(data),
    });
  } else {
    r = await fetch('/api/obs', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(data),
    });
  }

  const res = await r.json();
  if (res.error) { toast(res.error, 'error'); return; }
  toast(editingObsId ? '更新しました ✓' : '保存しました ✓', 'success');
  clearObsForm();
  loadObsList();
  refreshStatus();
}

let _obsAllRows = [];
let _obsActiveYM = null;

async function loadObsList() {
  const r    = await fetch('/api/obs');
  _obsAllRows = await r.json();
  document.getElementById('obs-list-count').textContent = `${_obsAllRows.length}件`;
  renderObsTabs();
}

function renderObsTabs() {
  const el = document.getElementById('obs-list');
  if (!_obsAllRows.length) {
    el.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px">データなし — フォームから入力してください</div>';
    return;
  }

  // 年月ごとに集計
  const ymMap = {};
  for (const row of _obsAllRows) {
    const ym = (row.date || '').slice(0, 7); // "2026-04"
    if (!ymMap[ym]) ymMap[ym] = [];
    ymMap[ym].push(row);
  }
  const ymKeys = Object.keys(ymMap).sort().reverse();

  if (!_obsActiveYM || !ymMap[_obsActiveYM]) _obsActiveYM = ymKeys[0];

  // 年ごとにグループ化してタブ生成
  const years = [...new Set(ymKeys.map(k => k.slice(0,4)))];
  let tabsHtml = '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">';
  for (const ym of ymKeys) {
    const [y, m] = ym.split('-');
    const active = ym === _obsActiveYM;
    const cnt    = ymMap[ym].length;
    tabsHtml += `<button onclick="selectObsYM('${ym}')" style="
      padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;cursor:pointer;border:1px solid;
      background:${active ? 'var(--accent)' : 'var(--bg-input)'};
      color:${active ? '#000' : 'var(--text-sub)'};
      border-color:${active ? 'var(--accent)' : 'var(--border)'}">${y}年${parseInt(m)}月 <span style="opacity:.7">${cnt}</span></button>`;
  }
  tabsHtml += '</div>';

  const rows = ymMap[_obsActiveYM] || [];
  const rowsHtml = rows.map(row => {
    const surfCls = row.surface === 'turf' ? 'turf' : 'dirt';
    const surfLbl = row.surface === 'turf' ? '芝' : 'ダ';
    const cv      = row.cushion_value  ? `CV ${row.cushion_value}` : '—';
    const mo      = row.moisture_rate  ? `${row.moisture_rate}%`   : '—';
    const cvWarn  = row.cushion_value && (parseFloat(row.cushion_value) < 3 || parseFloat(row.cushion_value) > 20);
    return `<div class="obs-row">
      <span class="obs-date">${row.date || '—'}</span>
      <span class="obs-venue">${row.venue_ja || row.venue || '—'}</span>
      <span class="obs-surf ${surfCls}">${surfLbl}</span>
      <span class="obs-cv${cvWarn ? ' obs-warn' : ''}">${cv} / ${mo}</span>
      <div class="obs-actions">
        <button class="btn-sm" onclick='editObs(${JSON.stringify(JSON.stringify(row))})'>編集</button>
        <button class="btn-sm del" onclick="deleteObs('${row.id.replace(/'/g,"\\'")}')">削除</button>
      </div>
    </div>`;
  }).join('');

  el.innerHTML = tabsHtml + '<div class="obs-list">' + rowsHtml + '</div>';
}

function selectObsYM(ym) {
  _obsActiveYM = ym;
  renderObsTabs();
}

function editObs(rowJson) {
  const row = JSON.parse(rowJson);
  loadObsIntoForm(row);
  switchTab('obs');
  document.querySelector('.card-body')?.scrollIntoView({behavior:'smooth'});
}

async function deleteObs(id) {
  if (!confirm(`削除しますか？\n${id}`)) return;
  const r   = await fetch(`/api/obs/${encodeURIComponent(id)}`, { method: 'DELETE' });
  const res = await r.json();
  if (res.error) { toast(res.error, 'error'); return; }
  toast('削除しました', 'success');
  if (editingObsId === id) cancelEditObs();
  loadObsList();
  refreshStatus();
}

async function exportObs() {
  window.location.href = '/api/obs/export';
}

async function fetchWeather() {
  const btn = document.getElementById('weather-btn');
  const res = document.getElementById('weather-result');
  btn.disabled = true;
  btn.textContent = '⏳ 取得中（数分かかります）...';
  res.style.display = 'none';
  try {
    const r    = await fetch('/api/fetch-weather', { method: 'POST', headers: {'Content-Type':'application/json'} });
    const data = await r.json();
    if (!r.ok) {
      res.style.background = 'rgba(239,68,68,0.15)'; res.style.borderColor = 'rgba(239,68,68,0.4)'; res.style.color = '#f87171';
      res.textContent = data.error || 'エラーが発生しました';
    } else {
      res.style.background = 'rgba(34,197,94,0.1)'; res.style.borderColor = 'rgba(34,197,94,0.3)'; res.style.color = '#4ade80';
      res.textContent = `✅ 完了 — ${data.updated}件に気象データを紐付けました`;
      if (data.updated > 0) await loadObsList();
    }
    res.style.display = 'block';
  } catch(e) {
    res.style.background = 'rgba(239,68,68,0.15)'; res.style.borderColor = 'rgba(239,68,68,0.4)'; res.style.color = '#f87171';
    res.textContent = 'ネットワークエラー: ' + e.message; res.style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = '🌤 気象データを取得・紐付け';
  }
}

async function fetchFromJRAPdf() {
  const btn  = document.getElementById('pdf-btn');
  const res  = document.getElementById('pdf-result');
  const yf   = parseInt(document.getElementById('pdf-year-from').value);
  const yt   = parseInt(document.getElementById('pdf-year-to').value);
  if (!yf || !yt || yf > yt) { toast('年度を正しく入力してください', 'error'); return; }
  btn.disabled = true;
  btn.textContent = '⏳ 取得中（数分かかります）...';
  res.style.display = 'none';
  try {
    const r    = await fetch('/api/fetch-jra-pdf', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({year_from: yf, year_to: yt})
    });
    const data = await r.json();
    if (!r.ok) {
      res.style.background = 'rgba(239,68,68,0.15)';
      res.style.borderColor = 'rgba(239,68,68,0.4)';
      res.style.color = '#f87171';
      res.textContent = data.error || 'エラーが発生しました';
    } else {
      res.style.background = 'rgba(34,197,94,0.1)';
      res.style.borderColor = 'rgba(34,197,94,0.3)';
      res.style.color = '#4ade80';
      res.textContent = `✅ 取得完了 — 新規: ${data.created}件 / スキップ: ${data.skipped}件`;
      if (data.created > 0) { await loadObsList(); }
    }
    res.style.display = 'block';
  } catch(e) {
    res.style.background = 'rgba(239,68,68,0.15)';
    res.style.borderColor = 'rgba(239,68,68,0.4)';
    res.style.color = '#f87171';
    res.textContent = 'ネットワークエラー: ' + e.message;
    res.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = '📥 PDFから過去データ取得';
  }
}

async function fetchFromJRA() {
  const btn  = document.getElementById('fetch-btn');
  const res  = document.getElementById('fetch-result');
  const date = document.getElementById('fetch-date').value;
  btn.disabled = true;
  btn.textContent = '⏳ 取得中...';
  res.style.display = 'none';
  try {
    const r    = await fetch('/api/fetch-jra', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(date ? {date} : {})
    });
    const data = await r.json();
    if (!r.ok) {
      res.style.background = 'rgba(239,68,68,0.15)';
      res.style.borderColor = 'rgba(239,68,68,0.4)';
      res.style.color = '#f87171';
      res.textContent = data.error || 'エラーが発生しました';
    } else {
      res.style.background = 'rgba(34,197,94,0.1)';
      res.style.borderColor = 'rgba(34,197,94,0.3)';
      res.style.color = '#4ade80';
      const datesStr = data.dates && data.dates.length ? ` (${data.dates.join(', ')})` : '';
      res.textContent = `✅ 取得完了${datesStr} — 新規: ${data.created}件 / スキップ: ${data.skipped}件`;
      if (data.created > 0) { await loadObsList(); }
    }
    res.style.display = 'block';
  } catch(e) {
    res.style.background = 'rgba(239,68,68,0.15)';
    res.style.borderColor = 'rgba(239,68,68,0.4)';
    res.style.color = '#f87171';
    res.textContent = 'ネットワークエラー: ' + e.message;
    res.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = '🤖 JRAからデータ取得';
  }
}

// ════════════════ Toast ════════════════

function toast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className   = `toast ${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => { el.remove(); }, 3500);
}

init();
</script>
</body></html>"""


if __name__ == '__main__':
    print()
    print('=' * 50)
    print('  keiba-scatter-v2 管理ダッシュボード')
    print('  http://localhost:5001/')
    print('  終了: Ctrl+C')
    print('=' * 50)
    print()
    app.run(host='127.0.0.1', port=5001, debug=False, threaded=True)
