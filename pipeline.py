#!/usr/bin/env python3
"""
競馬クッション値×含水率 散布図 一括生成パイプライン

使い方:
  python pipeline.py 20260215              # 全レース生成
  python pipeline.py 20260215 --venue 東京  # 東京のみ
  python pipeline.py 20260215 --race 11    # 全場の11Rのみ
  python pipeline.py 20260215 --deploy     # 生成後にGitHub Pagesへ自動デプロイ
"""

import requests
from bs4 import BeautifulSoup
import re
import time
import json
import os
import sys
import io
import argparse
import base64
import sqlite3
from datetime import datetime
from urllib.parse import quote

# Windowsでの日本語・特殊文字出力対応
if sys.stdout.encoding and sys.stdout.encoding.lower() in ('cp932', 'shift_jis', 'sjis'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ===== 設定 =====
CUSHION_DB_PATH = os.path.join(os.path.dirname(__file__), 'cushion_db_full.json')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
CACHE_DIR = os.path.join(os.path.dirname(__file__), 'cache')
HORSE_CACHE_DB = os.path.join(os.path.dirname(__file__), 'horse_results_cache.db')


# ===== 馬成績キャッシュ（SQLite） =====
def _cache_conn():
    os.makedirs(os.path.dirname(HORSE_CACHE_DB) or '.', exist_ok=True)
    conn = sqlite3.connect(HORSE_CACHE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS horse_results (
            horse_id   TEXT PRIMARY KEY,
            results_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

def _cache_get(horse_id: str):
    try:
        with _cache_conn() as conn:
            row = conn.execute(
                "SELECT results_json FROM horse_results WHERE horse_id = ?", (horse_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None
    except Exception:
        return None

def _cache_set(horse_id: str, results: list):
    try:
        with _cache_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO horse_results VALUES (?,?,?)",
                (horse_id, json.dumps(results, ensure_ascii=False), datetime.now().isoformat())
            )
            conn.commit()
    except Exception:
        pass

VENUE_CODES = {
    '01': '札幌', '02': '函館', '03': '福島', '04': '新潟',
    '05': '東京', '06': '中山', '07': '中京', '08': '京都',
    '09': '阪神', '10': '小倉'
}


# ===== Step 1: JRA ライブデータ取得 =====
def fetch_jra_live():
    """JRA公式からクッション値・含水率をリアルタイム取得"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    result = {}

    # クッション値
    r = requests.get('https://www.jra.go.jp/keiba/baba/_data_cushion.html', headers=headers)
    r.encoding = 'shift_jis'
    soup = BeautifulSoup(r.text, 'html.parser')
    for div in soup.find_all('div', id=re.compile(r'^rc[A-Z]')):
        venue = div.get('title', '')
        units = div.find_all('div', class_='unit')
        if units:
            cushion_text = units[0].find('div', class_='cushion').get_text(strip=True)
            time_text = units[0].find('div', class_='time').get_text(strip=True)
            result[venue] = {'cushion': float(cushion_text), 'time_cushion': time_text}

    # 含水率
    r = requests.get('https://www.jra.go.jp/keiba/baba/_data_moist.html', headers=headers)
    r.encoding = 'shift_jis'
    soup = BeautifulSoup(r.text, 'html.parser')
    for div in soup.find_all('div', id=re.compile(r'^rc[A-Z]')):
        venue = div.get('title', '')
        units = div.find_all('div', class_='unit')
        if units:
            u = units[0]
            turf_div = u.find('div', class_='turf')
            dirt_div = u.find('div', class_='dirt')
            turf_mg = float(turf_div.find('span', class_='mg').get_text(strip=True)) if turf_div else None
            dirt_mg = float(dirt_div.find('span', class_='mg').get_text(strip=True)) if dirt_div else None
            time_text = u.find('div', class_='time').get_text(strip=True)
            if venue in result:
                result[venue]['turf_moisture'] = turf_mg
                result[venue]['dirt_moisture'] = dirt_mg
                result[venue]['time_moisture'] = time_text
            else:
                result[venue] = {'turf_moisture': turf_mg, 'dirt_moisture': dirt_mg, 'time_moisture': time_text}

    # 天候（クッションページの天候divから取得試行）
    try:
        r2 = requests.get('https://www.jra.go.jp/keiba/baba/_data_cushion.html', headers=headers, timeout=8)
        r2.encoding = 'shift_jis'
        soup2 = BeautifulSoup(r2.text, 'html.parser')
        for div in soup2.find_all('div', id=re.compile(r'^rc[A-Z]')):
            venue = div.get('title', '')
            if not venue:
                continue
            weather_div = div.find('div', class_=re.compile(r'weather|tenki|Weather'))
            if not weather_div:
                # テキスト内から天候パターン検索
                text = div.get_text()
                wm = re.search(r'天候[：:]\s*(晴|曇|雨|小雨|雪|小雪)', text)
                if wm and venue in result:
                    result[venue]['weather'] = wm.group(1)
            else:
                w = weather_div.get_text(strip=True)
                if venue in result:
                    result[venue]['weather'] = w
    except Exception:
        pass

    return result


# ===== 会場別時間帯天気取得（Open-Meteo API） =====
VENUE_COORDS = {
    '東京':  (35.684, 139.773), '中山':  (35.778, 139.932),
    '阪神':  (34.723, 135.380), '京都':  (34.897, 135.753),
    '中京':  (35.196, 136.964), '福島':  (37.760, 140.474),
    '新潟':  (37.692, 139.020), '小倉':  (33.884, 130.877),
    '札幌':  (43.062, 141.355), '函館':  (41.774, 140.729),
}
WMO_EMOJI = {
    0:'☀️', 1:'🌤️', 2:'⛅', 3:'🌥️',
    45:'🌫️', 48:'🌫️',
    51:'🌦️', 53:'🌦️', 55:'🌦️',
    61:'🌧️', 63:'🌧️', 65:'🌧️',
    71:'🌨️', 73:'🌨️', 75:'🌨️',
    80:'🌧️', 81:'🌧️', 82:'🌧️',
    95:'⛈️', 96:'⛈️', 99:'⛈️',
}

def fetch_venue_weather(venues, date_str):
    """Open-Meteoから会場別 9/12/15時の天気コードを取得。{venue: [emoji9, emoji12, emoji15]}"""
    result = {}
    y, m, d = date_str[:4], date_str[4:6], date_str[6:]
    date_iso = f'{y}-{m}-{d}'
    for venue in venues:
        coord = VENUE_COORDS.get(venue)
        if not coord:
            continue
        lat, lon = coord
        try:
            url = (
                f'https://api.open-meteo.com/v1/forecast'
                f'?latitude={lat}&longitude={lon}'
                f'&hourly=weather_code&timezone=Asia%2FTokyo'
                f'&start_date={date_iso}&end_date={date_iso}'
            )
            r = requests.get(url, timeout=10)
            data = r.json()
            times = data['hourly']['time']
            codes = data['hourly']['weather_code']
            time_map = {t: c for t, c in zip(times, codes)}
            emojis = []
            for hour in ['09', '12', '15']:
                key = f'{date_iso}T{hour}:00'
                code = time_map.get(key, 0)
                emojis.append(WMO_EMOJI.get(code, '❓'))
            result[venue] = emojis
        except Exception:
            pass
    return result


# ===== 重賞グレード取得（netkeiba） =====
def fetch_grades_for_date(date_str):
    """指定日の重賞グレード {venue_RR: (race_name, grade)} を返す"""
    url = f'https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date_str}'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    grade_type_re = re.compile(r'Icon_GradeType(\d+)')
    grades = {}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')
        for li in soup.find_all('li', class_='bg_jyoken'):
            grade_span = li.find('span', class_=grade_type_re)
            if not grade_span:
                continue
            gm = grade_type_re.search(' '.join(grade_span.get('class', [])))
            if not gm:
                continue
            gtype = int(gm.group(1))
            if gtype not in (1, 2, 3):  # G1/G2/G3のみ（15はL）
                continue
            grade = f'G{gtype}'
            a = li.find('a', href=re.compile(r'race_id='))
            if not a:
                continue
            rid_m = re.search(r'race_id=(\d+)', a.get('href', ''))
            if not rid_m:
                continue
            rid = rid_m.group(1)
            venue = VENUE_CODES.get(rid[4:6], '?')
            rnum = int(rid[10:12])
            # レース名取得（RaceName_Text か テキストから）
            name_tag = li.find(class_=re.compile(r'RaceName'))
            if name_tag:
                rname = name_tag.get_text(strip=True)
            else:
                txt = li.get_text(' ', strip=True)
                nm = re.search(r'\d+R\s*(.+?)\s+\d{1,2}:\d{2}', txt)
                rname = nm.group(1).strip() if nm else ''
            grades[f'{venue}_{rnum:02d}'] = (rname, grade)
    except Exception:
        pass
    return grades


# ===== Step 2: レース一覧取得 =====
def get_race_list(date_str):
    """netkeiba からレース一覧取得 (date_str: YYYYMMDD)"""
    url = f'https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date_str}'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    r = requests.get(url, headers=headers)
    r.encoding = 'utf-8'
    soup = BeautifulSoup(r.text, 'html.parser')

    links = soup.find_all('a', href=re.compile(r'race_id=\d+'))
    seen_rid = set()
    seen_slot = set()  # (venue, race_num) で重複除去
    races = []
    for link in links:
        m = re.search(r'race_id=(\d+)', link.get('href', ''))
        if not m or m.group(1) in seen_rid:
            continue
        rid = m.group(1)
        seen_rid.add(rid)
        text = link.get_text(strip=True)
        venue_code = rid[4:6]
        venue = VENUE_CODES.get(venue_code, '?')
        race_num = int(rid[10:12])

        slot = (venue, race_num)
        if slot in seen_slot:
            continue
        seen_slot.add(slot)

        sd_match = re.search(r'(芝|ダ|障)(\d+)m', text)
        surface = sd_match.group(1) if sd_match else '?'
        distance = int(sd_match.group(2)) if sd_match else 0

        name_match = re.match(r'\d+R(.+?)\d{1,2}:\d{2}', text)
        race_name = name_match.group(1).strip() if name_match else text

        races.append({
            'race_id': rid,
            'venue': venue,
            'race_num': race_num,
            'race_name': race_name,
            'surface': surface,
            'distance': distance,
            'text': text,
        })

    return races


# ===== Step 3: 出走馬+過去成績取得 =====
def scrape_race_data(race_id):
    """netkeiba から出走馬と各馬の過去成績を取得"""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })

    # 出馬表取得
    url = f'https://race.netkeiba.com/race/shutuba.html?race_id={race_id}'
    r = session.get(url)
    r.encoding = 'euc-jp'
    soup = BeautifulSoup(r.text, 'html.parser')

    race_name_tag = soup.find('div', class_='RaceName')
    race_name = race_name_tag.get_text(strip=True) if race_name_tag else ''
    race_data_tag = soup.find('div', class_='RaceData01')
    race_data_text = race_data_tag.get_text(strip=True) if race_data_tag else ''
    sd_match = re.search(r'(芝|ダ|障)(\d+)m', race_data_text)
    surface = sd_match.group(1) if sd_match else '?'
    distance = int(sd_match.group(2)) if sd_match else 0
    time_match = re.search(r'(\d{1,2}):(\d{2})発走', race_data_text)
    start_time = f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else ''
    venue_code = race_id[4:6]
    venue = VENUE_CODES.get(venue_code, '?')

    # 馬一覧
    horses = []
    table = soup.find('table', class_='Shutuba_Table') or soup.find('table', id='shutuba_table')
    if not table:
        print(f"    WARNING: Shutuba table not found")
        return None

    rows = table.find_all('tr', class_='HorseList')
    for row in rows:
        horse_link = row.find('a', href=re.compile(r'/horse/\d+'))
        if not horse_link:
            continue
        horse_name = horse_link.get_text(strip=True)
        horse_id_match = re.search(r'/horse/(\d+)', horse_link.get('href', ''))
        horse_id = horse_id_match.group(1) if horse_id_match else None
        umaban_td = row.find('td', class_=re.compile(r'^Umaban\d*$'))
        horse_num = umaban_td.get_text(strip=True) if umaban_td else ''
        horses.append({'name': horse_name, 'horse_id': horse_id, 'horse_num': horse_num})

    # 各馬の過去成績
    all_horses = {}
    horse_nums = {}
    for h in horses:
        cached = _cache_get(h['horse_id']) if h['horse_id'] else None
        results = get_horse_results(session, h['horse_id'])
        all_horses[h['name']] = results
        horse_nums[h['name']] = h['horse_num']
        label = '(キャッシュ)' if cached is not None else '(取得)'
        print(f"    {h['name']}: {len(results)}走 {label}")
        if cached is None:
            time.sleep(1.0)

    return {
        'race_info': {
            'race_id': race_id,
            'race_name': race_name,
            'venue': venue,
            'surface': surface,
            'distance': distance,
            'start_time': start_time,
        },
        'horses': all_horses,
        'horse_nums': horse_nums,
    }


def get_horse_results(session, horse_id, max_races=10):
    """馬の過去成績を取得（SQLiteキャッシュ付き）"""
    if not horse_id:
        return []

    cached = _cache_get(horse_id)
    if cached is not None:
        return cached

    url = f'https://db.netkeiba.com/horse/result/{horse_id}/'
    r = session.get(url)
    r.encoding = 'euc-jp'
    soup = BeautifulSoup(r.text, 'html.parser')

    results = []
    table = soup.find('table', class_='db_h_race_results')
    if not table:
        return results

    venue_short_map = {
        '東': '東京', '京': '京都', '中': '中山', '阪': '阪神',
        '小': '小倉', '新': '新潟', '福': '福島', '函': '函館',
        '札': '札幌', '中京': '中京',
    }

    rows = table.find_all('tr')
    for tr in rows[1:max_races + 1]:
        cells = tr.find_all('td')
        if len(cells) < 15:
            continue
        try:
            date = cells[0].get_text(strip=True)
            venue_raw = cells[1].get_text(strip=True)
            race_name = cells[4].get_text(strip=True)
            result_text = cells[11].get_text(strip=True)
            result = int(result_text) if result_text.isdigit() else None
            dist_text = cells[14].get_text(strip=True)
            sd_match = re.search(r'(芝|ダ|障)(\d+)', dist_text)
            surface = sd_match.group(1) if sd_match else '?'
            distance = int(sd_match.group(2)) if sd_match else 0
            venue = re.sub(r'\d+', '', venue_raw).strip()
            for short, full in venue_short_map.items():
                if venue == short:
                    venue = full
                    break

            num_horses = cells[6].get_text(strip=True) if len(cells) > 6 else ''
            time_diff = cells[19].get_text(strip=True) if len(cells) > 19 else ''
            passage = cells[25].get_text(strip=True) if len(cells) > 25 else ''
            pace = cells[26].get_text(strip=True) if len(cells) > 26 else ''
            agari = cells[27].get_text(strip=True) if len(cells) > 27 else ''
            winner = cells[31].get_text(strip=True) if len(cells) > 31 else ''

            results.append({
                'date': date,
                'venue': venue,
                'surface': surface,
                'distance': distance,
                'race_name': race_name,
                'result': result,
                'num_horses': num_horses,
                'time_diff': time_diff,
                'passage': passage,
                'pace': pace,
                'agari': agari,
                'winner': winner,
            })
        except Exception:
            continue

    _cache_set(horse_id, results)
    return results


# ===== Step 4: クッション値紐付け =====
def link_cushion_data(race_data, cushion_db):
    """各馬の過去レースにクッション値・含水率を紐付け"""
    for horse_name, races in race_data['horses'].items():
        for r in races:
            date = r['date']
            venue = r['venue']
            surface = r.get('surface', '芝')

            key = f"{date}_{venue}"
            if key in cushion_db:
                entry = cushion_db[key]
                r['cushion'] = entry['cushion']
                if surface == 'ダ' or surface == 'ダート':
                    r['moisture'] = entry.get('dirt_goal')
                else:
                    r['moisture'] = entry.get('turf_goal')
            else:
                r['cushion'] = None
                r['moisture'] = None

    return race_data


# ===== Step 5: 散布図HTML生成 =====
def generate_scatter_html(race_data, target_cushion, target_moisture, output_path, date_label='', race_num=0, race_date=''):
    """散布図HTMLを生成"""
    race_info = race_data['race_info']
    venue = race_info['venue']
    race_name = race_info['race_name']
    surface = race_info['surface']
    distance = race_info['distance']

    horse_nums = race_data.get('horse_nums', {})
    js_horses = []
    for horse_name, races in race_data['horses'].items():
        js_races = []
        for r in races:
            # 当日レースの結果を除外（出走前分析のため）
            if race_date and r.get('date', '').replace('/', '') == race_date:
                continue
            if r.get('cushion') is None or r.get('moisture') is None:
                continue
            if r['surface'] != surface:
                cat = 'diff_surface'
            elif r['distance'] == distance:
                cat = 'same_dist'
            else:
                cat = 'diff_dist'

            js_races.append({
                'date': r['date'],
                'venue': r['venue'],
                'surface': r['surface'],
                'distance': r['distance'],
                'race_name': r['race_name'],
                'result': r['result'],
                'cushion': r['cushion'],
                'moisture': r['moisture'],
                'cat': cat,
                'good': r['result'] is not None and r['result'] <= 3,
                'num_horses': r.get('num_horses', ''),
                'time_diff': r.get('time_diff', ''),
                'passage': r.get('passage', ''),
                'pace': r.get('pace', ''),
                'agari': r.get('agari', ''),
                'winner': r.get('winner', ''),
            })
        js_horses.append({
            'name': horse_name,
            'horse_num': horse_nums.get(horse_name, ''),
            'races': js_races,
        })

    horses_json = json.dumps(js_horses, ensure_ascii=False)
    surface_label = '芝' if surface == '芝' else 'ダート'
    color_same = f'同距離{surface_label}'
    color_diff = f'他距離{surface_label}'
    color_other = 'ダート' if surface == '芝' else '芝レース'

    # --- AI読み取り用 構造化データ生成 ---
    structured_data = {
        'レース情報': {
            '開催日': date_label,
            '競馬場': venue,
            'レース番号': f'{race_num}R',
            'レース名': race_name,
            '馬場': surface_label,
            '距離': f'{distance}m',
            '当日クッション値': target_cushion,
            '当日含水率': f'{target_moisture}%',
        },
        '出走馬': []
    }
    for h in js_horses:
        horse_entry = {
            '馬名': h['name'],
            '過去走数': len(h['races']),
            '近似条件好走': 0,
            '過去レース': []
        }
        for r in h['races']:
            cv_diff = abs(r['cushion'] - target_cushion)
            m_diff = abs(r['moisture'] - target_moisture)
            is_ideal = cv_diff <= 0.2 and m_diff <= 1.5
            is_near = cv_diff <= 0.5 and m_diff <= 3.0
            if is_ideal and r['good']:
                horse_entry['近似条件好走'] += 1
            horse_entry['過去レース'].append({
                '日付': r['date'],
                '競馬場': r['venue'],
                '馬場': r['surface'],
                '距離': f"{r['distance']}m",
                'レース名': r['race_name'],
                '着順': r['result'],
                'クッション値': r['cushion'],
                '含水率': f"{r['moisture']}%",
                '近似条件': '◎' if is_ideal else ('○' if is_near else ''),
                '3着以内': '✓' if r['good'] else '',
            })
        structured_data['出走馬'].append(horse_entry)
    structured_json = json.dumps(structured_data, ensure_ascii=False, indent=2)

    # AI用テキストサマリー
    ai_text_lines = []
    ai_text_lines.append(f"【{date_label} {venue}{race_num}R {race_name} {surface_label}{distance}m】")
    ai_text_lines.append(f"当日条件: クッション値={target_cushion} 含水率={target_moisture}%")
    ai_text_lines.append("")
    for h in js_horses:
        ai_text_lines.append(f"▼ {h['name']}（過去{len(h['races'])}走）")
        for r in h['races']:
            cv_diff = abs(r['cushion'] - target_cushion)
            m_diff = abs(r['moisture'] - target_moisture)
            is_ideal = cv_diff <= 0.2 and m_diff <= 1.5
            mark = '◎' if is_ideal else ''
            result_str = f"{r['result']}着" if r['result'] is not None else '取消'
            ai_text_lines.append(f"  {r['date']} {r['venue']} {r['surface']}{r['distance']}m {r['race_name']} {result_str} CV={r['cushion']} 含水率={r['moisture']}% {mark}")
    ai_text_summary = '\n'.join(ai_text_lines)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>クッション値×含水率 - {venue}{race_name}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Noto Sans JP', sans-serif;
  background: linear-gradient(160deg,#1e3a72 0%,#162d58 50%,#1a3268 100%); color: #ddeeff; overflow: hidden; height: 100vh;
}}
.header {{
  background: linear-gradient(180deg,rgba(255,255,255,0.10) 0%,rgba(255,255,255,0.02) 100%),#2d4a68;
  border-bottom: 1px solid rgba(255,255,255,0.12);
  padding: 12px 16px; z-index: 100;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.2),0 4px 20px rgba(0,0,0,0.5); flex-shrink: 0;
}}
.header h1 {{ font-size: 16px; font-weight: 900; letter-spacing: -0.5px; color: #fff; }}
.header .sub {{ font-size: 11px; color: #a8c8e8; margin-top: 2px; }}
.header .target {{
  display: inline-flex; gap: 12px; margin-top: 4px;
  font-size: 11px; font-weight: 700; font-family: monospace;
}}
.header .target span {{
  background: linear-gradient(180deg,rgba(255,255,255,0.12) 0%,rgba(255,255,255,0.03) 100%),#1a5276;
  border: 1px solid rgba(255,255,255,0.2); border-top: 1px solid rgba(255,255,255,0.35);
  padding: 2px 8px; border-radius: 4px; color: #c8e4ff;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.15);
}}
.main {{ display: flex; flex-direction: column; flex: 1; overflow: hidden; }}
@media (min-width: 768px) {{ .main {{ flex-direction: row; }} }}
.chart-area {{ position: relative; width: 100%; height: 40vh; min-height: 250px; flex-shrink: 0; }}
@media (min-width: 768px) {{ .chart-area {{ flex: 1; height: 100%; }} }}
canvas {{ display: block; width: 100% !important; height: 100% !important; touch-action: pan-y; }}
.panel {{
  border-top: 1px solid rgba(255,255,255,0.12); overflow-y: auto; padding: 8px 8px 80px 8px; background: linear-gradient(180deg,rgba(255,255,255,0.04) 0%,rgba(0,0,0,0.05) 100%),#2d4a68;
  flex: 1;
}}
@media (min-width: 768px) {{
  .panel {{ width: 320px; border-top: none; border-left: 1px solid rgba(255,255,255,0.12); }}
}}
.horse-btn {{
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 10px 14px; margin-bottom: 4px;
  border: 1px solid rgba(255,255,255,0.16); border-top: 1px solid rgba(255,255,255,0.30);
  border-radius: 12px;
  background: linear-gradient(180deg,rgba(255,255,255,0.11) 0%,rgba(255,255,255,0.03) 100%),#1a5276;
  cursor: pointer; transition: all 0.2s; font-size: 14px; font-weight: 700; color: #ddeeff;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.20), 0 3px 12px rgba(0,0,0,0.35);
  -webkit-tap-highlight-color: transparent;
}}
.horse-btn:hover {{ background: linear-gradient(180deg,rgba(255,255,255,0.17) 0%,rgba(255,255,255,0.06) 100%),#1a5276; }}
.horse-btn:active {{ transform: scale(0.98); }}
.horse-btn.selected {{
  border-color: rgba(245,158,11,0.8); border-top-color: #f59e0b;
  background: linear-gradient(180deg,rgba(245,158,11,0.18) 0%,rgba(245,158,11,0.06) 100%),#1a5276;
  box-shadow: inset 0 1px 0 rgba(245,158,11,0.4), 0 0 0 1px rgba(245,158,11,0.3);
}}
.horse-btn .count {{ font-size: 10px; color: #7aa8c8; font-weight: 600; margin-left: auto; }}
.horse-btn .dot {{ width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; box-shadow: 0 0 4px currentColor; }}
.horse-btn .horse-num {{ font-size: 11px; font-weight: 700; color: #fff; background: linear-gradient(180deg,rgba(255,255,255,0.15) 0%,rgba(0,0,0,0.1) 100%),#2d4a68; border-radius: 4px; padding: 1px 5px; min-width: 20px; text-align: center; flex-shrink: 0; border: 1px solid rgba(255,255,255,0.2); }}
.rating-row {{
  display: flex; gap: 5px; padding: 4px 14px 10px 22px;
}}
.rating-btn {{
  width: 34px; height: 28px;
  border: 1px solid rgba(255,255,255,0.20); border-top: 1px solid rgba(255,255,255,0.35);
  border-radius: 7px;
  background: linear-gradient(180deg,rgba(255,255,255,0.10) 0%,rgba(255,255,255,0.02) 100%),#2d4a68;
  cursor: pointer; font-size: 12px; font-weight: 800;
  color: #a8c8e8; transition: all 0.15s; -webkit-tap-highlight-color: transparent;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.18), 0 2px 6px rgba(0,0,0,0.3);
}}
.rating-btn:active {{ transform: scale(0.92); }}
.rating-btn.rated-S {{ background: linear-gradient(180deg,rgba(255,255,255,0.2) 0%,rgba(255,255,255,0.05) 100%),#dc2626; border-color: rgba(255,255,255,0.3); color: #fff; box-shadow: inset 0 1px 0 rgba(255,255,255,0.3), 0 0 8px rgba(220,38,38,0.5); }}
.rating-btn.rated-A {{ background: linear-gradient(180deg,rgba(255,255,255,0.2) 0%,rgba(255,255,255,0.05) 100%),#f59e0b; border-color: rgba(255,255,255,0.3); color: #fff; box-shadow: inset 0 1px 0 rgba(255,255,255,0.3), 0 0 8px rgba(245,158,11,0.5); }}
.rating-btn.rated-B {{ background: linear-gradient(180deg,rgba(255,255,255,0.2) 0%,rgba(255,255,255,0.05) 100%),#3b82f6; border-color: rgba(255,255,255,0.3); color: #fff; box-shadow: inset 0 1px 0 rgba(255,255,255,0.3), 0 0 8px rgba(59,130,246,0.5); }}
.rating-btn.rated-C {{ background: linear-gradient(180deg,rgba(255,255,255,0.2) 0%,rgba(255,255,255,0.05) 100%),#22c55e; border-color: rgba(255,255,255,0.3); color: #fff; box-shadow: inset 0 1px 0 rgba(255,255,255,0.3), 0 0 8px rgba(34,197,94,0.5); }}
.rating-btn.rated-D {{ background: linear-gradient(180deg,rgba(255,255,255,0.15) 0%,rgba(255,255,255,0.03) 100%),#3a6d9a; border-color: rgba(255,255,255,0.25); color: #fff; box-shadow: inset 0 1px 0 rgba(255,255,255,0.2); }}
.race-mark-row {{ display:flex; gap:4px; margin-top:4px; justify-content:flex-end; }}
.race-mark-btn {{
  width:30px; height:26px;
  border:1px solid rgba(255,255,255,0.20); border-top:1px solid rgba(255,255,255,0.35);
  border-radius:6px;
  background: linear-gradient(180deg,rgba(255,255,255,0.10) 0%,rgba(255,255,255,0.02) 100%),#2d4a68;
  cursor:pointer; font-size:11px; font-weight:800; color:#a8c8e8;
  transition:all 0.15s; -webkit-tap-highlight-color:transparent; padding:0;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.18), 0 2px 6px rgba(0,0,0,0.3);
}}
.race-mark-btn:active {{ transform:scale(0.92); }}
.race-mark-btn.marked-◎ {{ background:linear-gradient(180deg,rgba(255,255,255,0.2) 0%,rgba(255,255,255,0.05) 100%),#dc2626; border-color:rgba(255,255,255,0.3); color:#fff; box-shadow:inset 0 1px 0 rgba(255,255,255,0.3),0 0 8px rgba(220,38,38,0.4); }}
.race-mark-btn.marked-○ {{ background:linear-gradient(180deg,rgba(255,255,255,0.2) 0%,rgba(255,255,255,0.05) 100%),#f59e0b; border-color:rgba(255,255,255,0.3); color:#fff; box-shadow:inset 0 1px 0 rgba(255,255,255,0.3),0 0 8px rgba(245,158,11,0.4); }}
.race-mark-btn.marked-× {{ background:linear-gradient(180deg,rgba(255,255,255,0.15) 0%,rgba(255,255,255,0.03) 100%),#3a6d9a; border-color:rgba(255,255,255,0.25); color:#fff; }}
.horse-detail {{ display: none; padding: 8px 4px; }}
.horse-detail.show {{ display: block; }}
.race-card {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 6px; }}
.race-item {{
  padding: 8px 10px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.14); border-top: 1px solid rgba(255,255,255,0.25);
  background: linear-gradient(180deg,rgba(255,255,255,0.08) 0%,rgba(255,255,255,0.01) 100%),#1a5276; font-size: 10px; cursor: pointer;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.15),0 2px 8px rgba(0,0,0,0.25);
}}
.race-item.ideal {{ background: linear-gradient(180deg,rgba(255,255,255,0.08) 0%,rgba(255,255,255,0.01) 100%),#0e4030; border-color: rgba(52,211,153,0.5); border-top-color: rgba(52,211,153,0.7); }}
.race-item.highlighted {{ border-color: #f59e0b; box-shadow: 0 0 0 2px rgba(245,158,11,0.3); }}
.race-item .date {{ color: #c8e4ff; font-weight: 600; font-family: monospace; }}
.race-item .rname {{ color: #ffffff; font-weight: 700; font-size: 10px; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-shadow: 0 1px 3px rgba(0,0,0,0.5); }}
.race-item .result {{ font-size: 13px; font-weight: 900; text-shadow: 0 1px 4px rgba(0,0,0,0.6); }}
.race-item .cond {{ color: #c8e4ff; font-weight: 700; }}
.legend {{
  display: flex; gap: 12px; padding: 8px 16px; font-size: 10px;
  font-weight: 700; color: #3a5880; border-top: 1px solid rgba(60,100,200,0.25); flex-wrap: wrap;
}}
.legend span {{ display: flex; align-items: center; gap: 4px; }}
.legend .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
.tooltip {{
  display: none; position: fixed; background: rgba(4,9,22,0.97); color: #c8d8ff;
  padding: 10px 14px; border-radius: 10px; font-size: 12px; line-height: 1.6;
  pointer-events: none; z-index: 200; max-width: 250px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.6); border: 1px solid rgba(80,130,220,0.3);
}}
.tooltip.show {{ display: block; }}
</style>
</head>
<body style="display:flex;flex-direction:column;">
<div class="header">
  <h1>{venue}{race_num}R {race_name} {surface}{distance}m</h1>
  <div class="sub">出走馬 クッション値×含水率 解析</div>
  <div class="target">
    <span>CV: <b style="color:#d97706">{target_cushion}</b></span>
    <span>含水率: <b style="color:#2563eb">{target_moisture}%</b></span>
    <span style="color:#94a3b8">{date_label} {venue}</span>
  </div>
</div>
<div class="main">
  <div class="chart-area"><canvas id="chart"></canvas><div class="tooltip" id="tooltip"></div></div>
  <div class="panel" id="panel"></div>
</div>
<div class="legend">
  <span><span class="dot" style="background:#dc2626"></span> {color_same}</span>
  <span><span class="dot" style="background:#2563eb"></span> {color_diff}</span>
  <span><span class="dot" style="background:#94a3b8"></span> {color_other}</span>
  <span>○ 3着以内 / × 4着以下</span>
</div>
<!-- AI読み取り用 構造化データ（Gemini/ChatGPT対応） -->
<script type="application/json" id="race-data">
{structured_json}
</script>
<div id="ai-readable" style="display:none" aria-hidden="true">
<pre>{ai_text_summary}</pre>
</div>
<script>
const HORSES = {horses_json};
const TX = {target_cushion};
const TY = {target_moisture};
const LINE_X = 9.5;
const LINE_Y = 12.0;
const TDIST = {distance};
const SURFACE = '{surface}';
const COLORS = {{ same_dist:'#f87171', diff_dist:'#60a5fa', diff_surface:'#cbd5e1', target:'#fbbf24' }};
const X_MIN = 7.0, X_MAX = 12.0;
const Y_MIN = 0, Y_MAX = 22;
let selectedHorses = new Set();
let highlightedPoints = new Set();
const canvas = document.getElementById('chart');
const ctx = canvas.getContext('2d');
const tooltipEl = document.getElementById('tooltip');
const hueStep = 360 / Math.max(HORSES.length, 1);
const horseColors = HORSES.map((_, i) => `hsl(${{i * hueStep}}, 65%, 55%)`);

function resize() {{
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); draw();
}}
function toCanvasX(v) {{ const p=50; const w=canvas.width/(window.devicePixelRatio||1)-p*2; return p+(v-X_MIN)/(X_MAX-X_MIN)*w; }}
function toCanvasY(v) {{ const pt=20,pb=40; const h=canvas.height/(window.devicePixelRatio||1)-pt-pb; return pt+(1-(v-Y_MIN)/(Y_MAX-Y_MIN))*h; }}

function draw() {{
  const W=canvas.width/(window.devicePixelRatio||1), H=canvas.height/(window.devicePixelRatio||1);
  ctx.clearRect(0,0,W,H);
  const bg=ctx.createLinearGradient(0,0,W,H);
  bg.addColorStop(0,'#1e3a72');
  bg.addColorStop(0.45,'#162d58');
  bg.addColorStop(1,'#0e1f3d');
  ctx.fillStyle=bg; ctx.fillRect(0,0,W,H);
  ctx.strokeStyle='rgba(60,100,200,0.18)'; ctx.lineWidth=1;
  for(let x=Math.ceil(X_MIN);x<=X_MAX;x+=0.5){{ const px=toCanvasX(x); ctx.beginPath();ctx.moveTo(px,20);ctx.lineTo(px,H-40);ctx.stroke(); ctx.fillStyle='#3a5880';ctx.font='10px monospace';ctx.textAlign='center';ctx.fillText(x.toFixed(1),px,H-25); }}
  for(let y=0;y<=Y_MAX;y+=2){{ const py=toCanvasY(y); ctx.beginPath();ctx.moveTo(50,py);ctx.lineTo(W-50,py);ctx.stroke(); ctx.fillStyle='#3a5880';ctx.font='10px monospace';ctx.textAlign='right';ctx.fillText(y+'%',45,py+4); }}
  ctx.fillStyle='#2a4060';ctx.font='bold 11px sans-serif';ctx.textAlign='center';
  ctx.fillText('クッション値',W/2,H-5);
  ctx.save();ctx.translate(12,H/2);ctx.rotate(-Math.PI/2);ctx.fillText('含水率（ゴール前）%',0,0);ctx.restore();
  ctx.setLineDash([6,3]);ctx.strokeStyle='rgba(100,150,255,0.45)';ctx.lineWidth=2;
  if(SURFACE==='芝'){{
    ctx.beginPath();ctx.moveTo(toCanvasX(LINE_X),20);ctx.lineTo(toCanvasX(LINE_X),H-40);ctx.stroke();
    ctx.beginPath();ctx.moveTo(50,toCanvasY(LINE_Y));ctx.lineTo(W-50,toCanvasY(LINE_Y));ctx.stroke();
  }}else{{
    [5,10,15].forEach(pct=>{{ctx.beginPath();ctx.moveTo(50,toCanvasY(pct));ctx.lineTo(W-50,toCanvasY(pct));ctx.stroke();}});
  }}
  ctx.setLineDash([]);
  const hlDeferred=[];
  HORSES.forEach((h,hi)=>{{
    const isSel=selectedHorses.has(h.name), dimmed=selectedHorses.size>0&&!isSel;
    const alpha=dimmed?0.08:(isSel?1.0:0.7);
    h.races.forEach((r,ri)=>{{
      const isHL=highlightedPoints.has(hi+'-'+ri);
      if(isHL){{hlDeferred.push({{h,hi,r,ri,isSel,dimmed,alpha}});return;}}
      const px=toCanvasX(r.cushion),py=toCanvasY(r.moisture),color=COLORS[r.cat];
      const sz=isSel?15:10;
      ctx.globalAlpha=alpha;
      if(r.good){{ ctx.beginPath();ctx.arc(px,py,sz,0,Math.PI*2);ctx.fillStyle='#162d58';ctx.fill();ctx.strokeStyle=color;ctx.lineWidth=isSel?3.5:2;ctx.stroke(); }}
      else{{ ctx.strokeStyle=color;ctx.lineWidth=isSel?3.5:2;ctx.beginPath();ctx.moveTo(px-sz,py-sz);ctx.lineTo(px+sz,py+sz);ctx.stroke();ctx.beginPath();ctx.moveTo(px+sz,py-sz);ctx.lineTo(px-sz,py+sz);ctx.stroke(); }}
      if(!dimmed){{ ctx.font=`bold ${{isSel?11:8}}px Arial`;ctx.textAlign='center';ctx.textBaseline='middle';ctx.strokeStyle='#060d20';ctx.lineWidth=3;ctx.strokeText(r.result||'?',px,py+1);ctx.fillStyle='#ffffff';ctx.fillText(r.result||'?',px,py+1); }}
    }});
  }});
  hlDeferred.forEach(({{h,hi,r,ri}})=>{{
    const px=toCanvasX(r.cushion),py=toCanvasY(r.moisture),color=COLORS[r.cat];
    const sz=18;
    ctx.globalAlpha=1.0;
    ctx.strokeStyle='#f59e0b';ctx.lineWidth=5;ctx.beginPath();ctx.arc(px,py,sz+6,0,Math.PI*2);ctx.stroke();
    if(r.good){{ ctx.beginPath();ctx.arc(px,py,sz,0,Math.PI*2);ctx.fillStyle='#162d58';ctx.fill();ctx.strokeStyle='#f59e0b';ctx.lineWidth=4;ctx.stroke(); }}
    else{{ ctx.strokeStyle='#f59e0b';ctx.lineWidth=4;ctx.beginPath();ctx.moveTo(px-sz,py-sz);ctx.lineTo(px+sz,py+sz);ctx.stroke();ctx.beginPath();ctx.moveTo(px+sz,py-sz);ctx.lineTo(px-sz,py+sz);ctx.stroke(); }}
    ctx.font='bold 13px Arial';ctx.textAlign='center';ctx.textBaseline='middle';ctx.strokeStyle='#060d20';ctx.lineWidth=3;ctx.strokeText(r.result||'?',px,py+1);ctx.fillStyle='#ffffff';ctx.fillText(r.result||'?',px,py+1);
  }});
  ctx.globalAlpha=1;
  const RANK_COLORS={{S:'#dc2626',A:'#f59e0b',B:'#3b82f6',C:'#22c55e',D:'#94a3b8'}};
  HORSES.forEach((h,hi)=>{{
    if(ratings[h.name]){{
      const rank=ratings[h.name];const rc=RANK_COLORS[rank];
      h.races.forEach(r=>{{
        const px=toCanvasX(r.cushion),py=toCanvasY(r.moisture);
        ctx.globalAlpha=selectedHorses.size>0&&!selectedHorses.has(h.name)?0.15:1;
        ctx.fillStyle=rc;ctx.font='bold 9px Arial';ctx.textAlign='left';
        ctx.fillText(rank,px+10,py-8);
      }});
    }}
  }});
  ctx.globalAlpha=1;
  const tx=toCanvasX(TX),ty=toCanvasY(TY);
  ctx.fillStyle=COLORS.target;ctx.font='bold 22px Arial';ctx.textAlign='center';ctx.textBaseline='middle';
  ctx.strokeStyle='#fff';ctx.lineWidth=3;ctx.strokeText('★',tx,ty);ctx.fillText('★',tx,ty);
  ctx.textBaseline='alphabetic';
}}
const STORAGE_KEY='ratings_{venue}_{race_num}R_{race_name}';
const ratings=(function(){{try{{const s=localStorage.getItem(STORAGE_KEY);return s?JSON.parse(s):{{}};}}catch(e){{return {{}};}}}})();
function saveRatings(){{try{{localStorage.setItem(STORAGE_KEY,JSON.stringify(ratings));}}catch(e){{}}}};
const RR_KEY='raceRatings_{venue}_{race_num}R_{race_name}';
const raceRatings=(function(){{try{{const s=localStorage.getItem(RR_KEY);return s?JSON.parse(s):{{}};}}catch(e){{return {{}};}}}})();
function saveRaceRatings(){{try{{localStorage.setItem(RR_KEY,JSON.stringify(raceRatings));}}catch(e){{}}}};
function buildPanel(){{
  const panel=document.getElementById('panel');
  const RANKS=['S','A','B','C','D'];
  let html='';
  HORSES.forEach((h,i)=>{{
    const cnt=h.races.length;
    html+=`<button class="horse-btn" id="btn-${{i}}"><span class="dot" style="background:${{horseColors[i]}}"></span><span class="horse-num">${{h.horse_num}}</span>${{h.name}}<span class="count">${{cnt>0?cnt+'走':'データなし'}}</span></button>`;
    html+=`<div class="rating-row" id="rate-${{i}}">`;
    RANKS.forEach(r=>{{html+=`<button class="rating-btn" data-horse="${{i}}" data-rank="${{r}}">${{r}}</button>`;}});
    html+=`</div>`;
    html+=`<div class="horse-detail" id="detail-${{i}}"><div class="race-card">${{h.races.map((r,ri)=>{{const inIdeal=Math.abs(r.cushion-TX)<=0.2&&Math.abs(r.moisture-TY)<=1.5;const rrKey=h.name+'_'+ri;return`<div class="race-item ${{inIdeal?'ideal':''}}" data-horse="${{i}}" data-ri="${{ri}}"><div class="date">${{r.date}} ${{r.venue}}</div><div class="rname">${{r.race_name}}</div><div class="cond">${{r.surface}}${{r.distance}}m ${{r.distance===TDIST?'(同)':r.distance>TDIST?'(短)':'(延)'}}</div><div style="display:flex;justify-content:space-between;align-items:center;margin-top:2px"><span style="font-size:9px;color:#b8d8f8">CV${{r.cushion}} / ${{r.moisture}}%</span><span style="font-size:9px;color:#b8d8f8;text-align:right">${{r.winner?`${{r.winner}}${{r.time_diff?'('+r.time_diff+')':''}} `:''}}<span class="result" style="color:${{COLORS[r.cat]}}">${{r.result!==null?r.result+'着':'取消'}}</span></span></div><div style="display:flex;justify-content:space-between;align-items:center;margin-top:2px"><span style="font-size:9px;color:#b8d8f8">${{r.num_horses?r.num_horses+'頭':''}}${{r.passage?'・'+r.passage:''}}</span><div class="race-mark-row" data-rrkey="${{rrKey}}"><button class="race-mark-btn" data-mark="◎">◎</button><button class="race-mark-btn" data-mark="○">○</button><button class="race-mark-btn" data-mark="×">×</button></div></div>${{r.agari?`<div style="font-size:9px;color:#b8d8f8;margin-top:1px">${{r.agari}}</div>`:''}}</div>`}}).join('')}}</div></div>`;
  }});
  panel.innerHTML=html;
  HORSES.forEach((h,i)=>{{document.getElementById('btn-'+i).addEventListener('click',()=>{{
    const detail=document.getElementById('detail-'+i);
    if(selectedHorses.has(h.name)){{selectedHorses.delete(h.name);detail.classList.remove('show');document.getElementById('btn-'+i).classList.remove('selected');}}
    else{{selectedHorses.add(h.name);detail.classList.add('show');document.getElementById('btn-'+i).classList.add('selected');}}
    requestAnimationFrame(()=>{{draw();}});
  }});}});
  document.querySelectorAll('.rating-btn').forEach(btn=>{{
    btn.addEventListener('click',(e)=>{{
      e.stopPropagation();
      const hi=parseInt(btn.dataset.horse);
      const rank=btn.dataset.rank;
      const name=HORSES[hi].name;
      if(ratings[name]===rank){{delete ratings[name];}}
      else{{ratings[name]=rank;}}
      updateRatings();
    }});
  }});
  document.querySelectorAll('.race-item').forEach(el=>{{
    el.addEventListener('click',(e)=>{{
      if(e.target.classList.contains('race-mark-btn'))return;
      e.stopPropagation();
      el.classList.toggle('highlighted');
      const key=el.dataset.horse+'-'+el.dataset.ri;
      if(highlightedPoints.has(key))highlightedPoints.delete(key);else highlightedPoints.add(key);
      requestAnimationFrame(()=>{{draw();}});
    }});
  }});
  document.querySelectorAll('.race-mark-btn').forEach(btn=>{{
    btn.addEventListener('click',(e)=>{{
      e.stopPropagation();
      const row=btn.closest('.race-mark-row');
      const rrKey=row.dataset.rrkey;
      const mark=btn.dataset.mark;
      if(raceRatings[rrKey]===mark){{delete raceRatings[rrKey];}}
      else{{raceRatings[rrKey]=mark;}}
      updateRaceMarks();
      saveRaceRatings();
    }});
  }});
}}
function updateRaceMarks(){{
  document.querySelectorAll('.race-mark-btn').forEach(btn=>{{
    const row=btn.closest('.race-mark-row');
    const rrKey=row.dataset.rrkey;
    const mark=btn.dataset.mark;
    btn.className='race-mark-btn'+(raceRatings[rrKey]===mark?' marked-'+mark:'');
  }});
}}
function updateRatings(){{
  document.querySelectorAll('.rating-btn').forEach(btn=>{{
    const hi=parseInt(btn.dataset.horse);
    const rank=btn.dataset.rank;
    const name=HORSES[hi].name;
    btn.className='rating-btn'+(ratings[name]===rank?' rated-'+rank:'');
  }});
  saveRatings();
  draw();
}}
const isMobile='ontouchstart' in window;
function getPointAt(cx,cy){{
  let closest=null,minDist=isMobile?35:20;
  HORSES.forEach(h=>{{if(selectedHorses.size>0&&!selectedHorses.has(h.name))return;h.races.forEach(r=>{{const px=toCanvasX(r.cushion),py=toCanvasY(r.moisture),d=Math.sqrt((cx-px)**2+(cy-py)**2);if(d<minDist){{minDist=d;closest={{...r,horse:h.name}};}}}});}});
  return closest;
}}
canvas.addEventListener('mousemove',(e)=>{{const rect=canvas.getBoundingClientRect();const x=e.clientX-rect.left,y=e.clientY-rect.top;const pt=getPointAt(x,y);if(pt){{tooltipEl.innerHTML=`<b>${{pt.horse}}</b><br>${{pt.date}} ${{pt.venue}} ${{pt.surface}}${{pt.distance}}m<br>${{pt.race_name}}<br><b>${{pt.result}}着</b><br>CV: ${{pt.cushion}} / 含水率: ${{pt.moisture}}%`;tooltipEl.style.left=(e.clientX+15)+'px';tooltipEl.style.top=(e.clientY-10)+'px';tooltipEl.classList.add('show');}}else{{tooltipEl.classList.remove('show');}}}});
canvas.addEventListener('mouseleave',()=>tooltipEl.classList.remove('show'));
let touchTimer=null;
function showTooltipAt(cx,cy,tx,ty){{const pt=getPointAt(cx,cy);if(pt){{tooltipEl.innerHTML=`<b>${{pt.horse}}</b><br>${{pt.date}} ${{pt.venue}} ${{pt.surface}}${{pt.distance}}m<br>${{pt.race_name}}<br><b>${{pt.result!==null?pt.result+'着':'取消'}}</b><br>CV: ${{pt.cushion}} / 含水率: ${{pt.moisture}}%`;const left=Math.min(tx+15,window.innerWidth-260);const top=Math.max(ty-40,10);tooltipEl.style.left=left+'px';tooltipEl.style.top=top+'px';tooltipEl.classList.add('show');}}else{{tooltipEl.classList.remove('show');}}}}
canvas.addEventListener('touchstart',(e)=>{{const t=e.touches[0];const rect=canvas.getBoundingClientRect();showTooltipAt(t.clientX-rect.left,t.clientY-rect.top,t.clientX,t.clientY);}},{{passive:true}});
canvas.addEventListener('touchmove',(e)=>{{const t=e.touches[0];const rect=canvas.getBoundingClientRect();showTooltipAt(t.clientX-rect.left,t.clientY-rect.top,t.clientX,t.clientY);}},{{passive:true}});
canvas.addEventListener('touchend',()=>{{if(touchTimer)clearTimeout(touchTimer);touchTimer=setTimeout(()=>tooltipEl.classList.remove('show'),2000);}});
canvas.addEventListener('click',(e)=>{{const rect=canvas.getBoundingClientRect();showTooltipAt(e.clientX-rect.left,e.clientY-rect.top,e.clientX,e.clientY);}});
window.addEventListener('resize',resize); buildPanel(); updateRatings(); updateRaceMarks(); resize();
</script>
</body></html>"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    total_pts = sum(len(h['races']) for h in js_horses)
    horses_with_data = sum(1 for h in js_horses if h['races'])
    return total_pts, horses_with_data, len(js_horses)


# ===== メインパイプライン =====
def main():
    parser = argparse.ArgumentParser(description='競馬クッション値×含水率 散布図 一括生成')
    parser.add_argument('date', help='開催日 (YYYYMMDD)')
    parser.add_argument('--venue', help='競馬場で絞り込み (東京/京都/小倉 等)')
    parser.add_argument('--race', type=int, help='レース番号で絞り込み (例: 11)')
    parser.add_argument('--no-scrape', action='store_true', help='キャッシュ済みデータのみ使用')
    parser.add_argument('--output', default=None, help='出力先ディレクトリ')
    parser.add_argument('--deploy', action='store_true', help='GitHub Pagesへ自動デプロイ')
    parser.add_argument('--manual', action='store_true', help='クッション値・含水率を会場別に手入力')
    parser.add_argument('--force-update', action='store_true', help='既存キーの上書きを許可')
    parser.add_argument('--cleanup', action='store_true', help='旧フォーマットファイルをGitHubから削除')
    args = parser.parse_args()

    date_str = args.date
    _weekdays = ['月', '火', '水', '木', '金', '土', '日']
    _dt = datetime(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]))
    date_label = f"{int(date_str[4:6])}/{int(date_str[6:])}（{_weekdays[_dt.weekday()]}）"
    out_dir = args.output or os.path.join(OUTPUT_DIR, date_str)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Step 1: レース一覧取得
    print("=" * 60)
    print(f"[Step 1] レース一覧取得 ({date_str})")
    print("=" * 60)
    races = get_race_list(date_str)

    # フィルタリング
    if args.venue:
        races = [r for r in races if r['venue'] == args.venue]
    if args.race:
        races = [r for r in races if r['race_num'] == args.race]

    # 障害レースを除外
    races = [r for r in races if r['surface'] != '障']

    print(f"  対象: {len(races)}レース")
    for r in races:
        print(f"    {r['venue']}{r['race_num']}R {r['race_name']} {r['surface']}{r['distance']}m")
    print()

    # Step 2: クッション値・含水率
    print("=" * 60)
    print(f"[Step 2] クッション値・含水率 取得")
    print("=" * 60)
    manual_mode = args.manual
    if manual_mode:
        venues_in_races = sorted(set(r['venue'] for r in races))
        jra_live = {}
        print(f"  *** 手入力モード ({len(venues_in_races)}会場) ***")
        print()
        for v in venues_in_races:
            print(f"  [{v}]")
            cv = input(f"    クッション値 (例: 9.5): ")
            mt = input(f"    芝 含水率% (例: 12.0): ")
            md = input(f"    ダート 含水率% (例: 5.0): ")
            jra_live[v] = {
                'cushion': float(cv),
                'turf_moisture': float(mt),
                'dirt_moisture': float(md),
            }
            print(f"    → CV={cv} 芝={mt}% ダ={md}%")
            print()
    else:
        jra_live = fetch_jra_live()
        for venue, data in jra_live.items():
            c = data.get('cushion', '?')
            tm = data.get('turf_moisture', '?')
            dm = data.get('dirt_moisture', '?')
            print(f"  {venue}: CV={c}  芝={tm}%  ダ={dm}%")
    print()

    # Step 3: クッション値DB読み込み
    print("=" * 60)
    print(f"[Step 3] クッション値DB読み込み")
    print("=" * 60)
    with open(CUSHION_DB_PATH, encoding='utf-8') as f:
        cushion_db = json.load(f)
    print(f"  DB件数: {len(cushion_db)}")

    # 当日データをDBに自動蓄積
    date_fmt = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:8]}"
    today = datetime.now().strftime('%Y%m%d')
    is_today = (date_str == today)
    added = 0

    if not is_today and not manual_mode:
        print(f"  ※ 指定日({date_str})は今日({today})ではないためDB蓄積をスキップします")
        print(f"    （JRAライブ値は今日の値であり、{date_str}の正しい値ではありません）")
        print(f"    散布図の表示にはライブ値をそのまま使用します")
    else:
        for venue, data in jra_live.items():
            key = f"{date_fmt}_{venue}"
            cushion_val = data.get('cushion')

            if cushion_val is None or cushion_val == 0.0:
                print(f"  ※ 警告: {venue}のクッション値が不正({cushion_val})のためDB保存をスキップします")
                continue

            if key not in cushion_db or args.force_update:
                if key in cushion_db and args.force_update:
                    print(f"  ※ {key} を上書きします（--force-update）")
                cushion_db[key] = {
                    'date': date_fmt,
                    'venue': venue,
                    'cushion': cushion_val,
                    'turf_goal': data.get('turf_moisture'),
                    'dirt_goal': data.get('dirt_moisture'),
                }
                added += 1
        if added > 0:
            with open(CUSHION_DB_PATH, 'w', encoding='utf-8') as f:
                json.dump(cushion_db, f, ensure_ascii=False, indent=2)
            print(f"  ✓ {added}件追加（DB更新: {len(cushion_db)}件）")
        else:
            print(f"  ✓ 既存データのため追加なし")
    print()

    # Step 4: 各レース処理
    print("=" * 60)
    print(f"[Step 4] 各レース処理")
    print("=" * 60)
    results_summary = []
    start_times_map = {}  # {venue_rnum: "HH:MM"}

    for race in races:
        rid = race['race_id']
        venue = race['venue']
        race_num = race['race_num']
        surface = race['surface']

        print(f"\n--- {venue} {race_num}R {race['race_name']} {surface}{race['distance']}m ---")

        cache_file = os.path.join(CACHE_DIR, f'race_{rid}.json')
        if args.no_scrape and os.path.exists(cache_file):
            print(f"  キャッシュ使用: {cache_file}")
            with open(cache_file, encoding='utf-8') as f:
                race_data = json.load(f)
        else:
            print(f"  netkeiba スクレイピング中...")
            race_data = scrape_race_data(rid)
            if race_data is None:
                print(f"  SKIP: データ取得失敗")
                continue
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(race_data, f, ensure_ascii=False, indent=2)
            print(f"  キャッシュ保存: {cache_file}")

        if not race_data.get('race_info', {}).get('race_name'):
            race_data.setdefault('race_info', {})['race_name'] = race['race_name']
        if not race_data['race_info'].get('surface'):
            race_data['race_info']['surface'] = surface
        if not race_data['race_info'].get('distance'):
            race_data['race_info']['distance'] = race['distance']

        # 発走時刻を収集
        st = race_data['race_info'].get('start_time', '')
        if st:
            start_times_map[f'{venue}_{race_num:02d}'] = st

        # クッション値紐付け
        race_data = link_cushion_data(race_data, cushion_db)

        # 当日クッション値・含水率（過去日はDBから取得）
        db_key = f"{date_fmt}_{venue}"
        if not is_today and db_key in cushion_db:
            db_entry = cushion_db[db_key]
            target_cushion = db_entry.get('cushion', 9.5)
            target_moisture = db_entry.get('dirt_goal' if surface == 'ダ' else 'turf_goal', 12.0) or 12.0
        else:
            target_cushion = jra_live.get(venue, {}).get('cushion', 9.5)
            if surface == 'ダ':
                target_moisture = jra_live.get(venue, {}).get('dirt_moisture', 5.0)
            else:
                target_moisture = jra_live.get(venue, {}).get('turf_moisture', 12.0)

        raw_name = race['race_name']
        clean_name = re.sub(r'(芝|ダ|障)\d+m', '', raw_name)
        clean_name = re.sub(r'\d+頭', '', clean_name)
        clean_name = re.sub(r'^0?\d+R', '', clean_name)
        safe_name = clean_name.strip().replace('/', '_').replace(' ', '')
        if not safe_name:
            safe_name = raw_name.replace('/', '_').replace(' ', '')
        output_file = os.path.join(out_dir, f'scatter_{date_str}_{venue}{race_num:02d}R_{safe_name}_{surface}{race["distance"]}m.html')

        new_basename = os.path.basename(output_file)
        prefix = f'scatter_{date_str}_{venue}{race_num:02d}R_'
        for old_f in os.listdir(out_dir):
            if old_f.startswith(prefix) and old_f.endswith('.html') and old_f != new_basename:
                os.remove(os.path.join(out_dir, old_f))
                print(f"  旧ファイル削除: {old_f}")

        pts, with_data, total = generate_scatter_html(
            race_data, target_cushion, target_moisture,
            output_file, date_label=date_label, race_num=race_num,
            race_date=date_str,
        )
        print(f"  ✓ 生成完了: {total}頭 ({with_data}頭データあり) {pts}ポイント")
        print(f"  → {output_file}")
        results_summary.append((venue, race_num, race['race_name'], total, pts, surface, race['distance']))

    # サマリー
    print()
    print("=" * 60)
    print("完了サマリー")
    print("=" * 60)
    for venue, rnum, rname, total, pts, surf, dist in results_summary:
        print(f"  {venue}{rnum:2d}R {rname:20s} {surf}{dist}m {total}頭 {pts}pts")
    print(f"\n  出力先: {out_dir}")
    print(f"  合計: {len(results_summary)}レース")

    # 発走時刻をJSONに保存
    if start_times_map:
        st_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               f'start_times_{date_str}.json')
        with open(st_path, 'w', encoding='utf-8') as f:
            json.dump(start_times_map, f, ensure_ascii=False, indent=2)
        print(f"  発走時刻保存: {st_path} ({len(start_times_map)}件)")

    # インデックスページ生成
    generate_index(out_dir, results_summary, jra_live, date_label, date_str)

    # デプロイ
    if args.deploy:
        deploy_to_github(out_dir, date_str, cleanup=args.cleanup)


def generate_index(out_dir, results_summary, jra_live, date_label, date_str=''):
    """レース一覧インデックスページを生成（会場横並びレイアウト）"""
    venues = {}
    for venue, rnum, rname, total, pts, surf, dist in results_summary:
        if venue not in venues:
            venues[venue] = []
        venues[venue].append((rnum, rname, total, pts, surf, dist))

    # 当日はライブ値、過去日はDBから取得
    today = datetime.now().strftime('%Y%m%d')
    is_today = (date_str == today)
    venue_info = {}
    if is_today:
        for venue, data in jra_live.items():
            c = data.get('cushion', '?')
            tm = data.get('turf_moisture', '?')
            dm = data.get('dirt_moisture', '?')
            venue_info[venue] = {'cushion': c, 'turf': tm, 'dirt': dm}
    else:
        cushion_db = {}
        if os.path.exists(CUSHION_DB_PATH):
            with open(CUSHION_DB_PATH, encoding='utf-8') as f:
                cushion_db = json.load(f)
        date_fmt = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:8]}"
        for venue in set(v for v, *_ in results_summary):
            key = f"{date_fmt}_{venue}"
            if key in cushion_db:
                e = cushion_db[key]
                venue_info[venue] = {
                    'cushion': e.get('cushion', '?'),
                    'turf': e.get('turf_goal', '?'),
                    'dirt': e.get('dirt_goal', '?'),
                }

    html = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{date_label} クッション値×含水率 散布図</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Noto Sans JP',sans-serif; background:linear-gradient(160deg,#1e3a72 0%,#162d58 50%,#1a3268 100%); color:#ddeeff; min-height:100vh; }}
.header {{ padding:16px 20px 12px; border-bottom:1px solid rgba(255,255,255,0.12); background:linear-gradient(180deg,rgba(255,255,255,0.08) 0%,rgba(255,255,255,0.01) 100%),#2d4a68; box-shadow:inset 0 1px 0 rgba(255,255,255,0.18); }}
.header h1 {{ font-size:18px; font-weight:900; color:#fff; }}
.header .date {{ font-size:13px; color:#a8c8e8; margin-top:2px; }}
.grid {{ display:flex; gap:12px; padding:16px; overflow-x:auto; align-items:flex-start; }}
.venue-col {{ flex:0 0 220px; background:linear-gradient(180deg,rgba(255,255,255,0.07) 0%,rgba(255,255,255,0.01) 100%),#2d4a68; border:1px solid rgba(255,255,255,0.14); border-top:1px solid rgba(255,255,255,0.25); border-radius:12px; overflow:hidden; box-shadow:inset 0 1px 0 rgba(255,255,255,0.15),0 4px 16px rgba(0,0,0,0.3); }}
.venue-head {{ padding:12px 14px 10px; background:linear-gradient(180deg,rgba(255,255,255,0.12) 0%,rgba(255,255,255,0.03) 100%),#3a6d9a; border-bottom:1px solid rgba(255,255,255,0.12); box-shadow:inset 0 1px 0 rgba(255,255,255,0.22); }}
.venue-head h2 {{ font-size:15px; font-weight:800; color:#fff; }}
.cv-info {{ margin-top:4px; font-size:10px; color:#c8e0f8; font-weight:600; }}
.cv-val {{ color:#fbbf24; font-weight:700; }}
.race-list {{ padding:6px 0; }}
a {{ display:flex; align-items:center; gap:8px; padding:9px 14px; color:#ddeeff; text-decoration:none; font-size:13px; font-weight:700; border-bottom:1px solid rgba(255,255,255,0.07); transition:background 0.15s; }}
a:last-child {{ border-bottom:none; }}
a:active, a:hover {{ background:rgba(255,255,255,0.08); }}
.rnum {{ font-size:11px; font-weight:800; color:#64748b; min-width:22px; }}
.rname {{ flex:1; font-size:13px; }}
.surf-badge {{ font-size:10px; font-weight:800; padding:2px 6px; border-radius:5px; flex-shrink:0; }}
.surf-turf {{ background:#166534; color:#86efac; }}
.surf-dirt {{ background:#78350f; color:#fcd34d; }}
.dist {{ font-size:11px; color:#64748b; flex-shrink:0; }}
</style>
</head>
<body>
<div class="header">
  <h1>クッション値×含水率 散布図</h1>
  <div class="date">{date_label}</div>
</div>
<div class="grid">
'''

    for venue in ['東京', '京都', '小倉', '中山', '阪神', '中京', '新潟', '福島', '函館', '札幌']:
        if venue not in venues:
            continue
        info = venue_info.get(venue, {})
        cv = info.get('cushion', '?')
        turf = info.get('turf', '?')
        dirt = info.get('dirt', '?')
        html += f'''<div class="venue-col">
<div class="venue-head">
  <h2>{venue}</h2>
  <div class="cv-info">CV <span class="cv-val">{cv}</span> &nbsp;芝{turf}% &nbsp;ダ{dirt}%</div>
</div>
<div class="race-list">
'''
        for rnum, rname, total, pts, surf, dist in sorted(venues[venue]):
            safe_name = rname.replace('/', '_').replace(' ', '')
            fname = f'scatter_{date_str}_{venue}{rnum:02d}R_{safe_name}_{surf}{dist}m.html'
            badge_cls = 'surf-turf' if surf == '芝' else 'surf-dirt'
            html += f'<a href="{fname}"><span class="rnum">{rnum}R</span><span class="rname">{rname}</span><span class="surf-badge {badge_cls}">{surf}</span><span class="dist">{dist}m</span></a>\n'
        html += '</div></div>\n'

    html += '</div></body></html>'

    index_path = os.path.join(out_dir, 'index.html')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n  インデックス: {index_path}")


# ===== GitHub Pages デプロイ =====
DEPLOY_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'deploy_config.json')

def deploy_to_github(out_dir, date_str, cleanup=False):
    """GitHub Pages へ自動デプロイ（GitHub API使用、git不要）"""
    print()
    print("=" * 60)
    print("[Deploy] GitHub Pages へデプロイ")
    print("=" * 60)

    if not os.path.exists(DEPLOY_CONFIG_PATH):
        print("  deploy_config.json が見つかりません。")
        print("  以下の形式で作成してください:")
        print('  {"github_token": "ghp_xxx", "repo": "user/repo-name"}')
        return

    with open(DEPLOY_CONFIG_PATH, encoding='utf-8') as f:
        config = json.load(f)

    cushion_db = {}
    if os.path.exists(CUSHION_DB_PATH):
        with open(CUSHION_DB_PATH, encoding='utf-8') as f:
            cushion_db = json.load(f)

    # JRAライブデータ（天候取得のため）
    jra_live = {}
    try:
        jra_live = fetch_jra_live()
    except Exception:
        pass

    # start_times_{YYYYMMDD}.json を全て読み込む
    base_dir = os.path.dirname(os.path.abspath(__file__))
    all_start_times = {}  # {date_str: {"会場_01": "HH:MM", ...}}
    for fn in os.listdir(base_dir):
        m = re.match(r'start_times_(\d{8})\.json', fn)
        if m:
            d = m.group(1)
            try:
                with open(os.path.join(base_dir, fn), encoding='utf-8') as f:
                    all_start_times[d] = json.load(f)
            except Exception:
                pass

    # 重賞グレードキャッシュ（日付ごとに取得）
    all_grades = {}  # {date_str: {"会場_01": (race_name, grade), ...}}

    token = config['github_token']
    repo = config['repo']
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
    }
    api_base = f'https://api.github.com/repos/{repo}/contents'

    print(f"  リポジトリ: {repo}")
    r = requests.get(api_base, headers=headers)
    existing = {}
    if r.status_code == 200:
        for item in r.json():
            existing[item['name']] = item['sha']

    html_files = [f for f in os.listdir(out_dir) if f.endswith('.html')]
    for fname in sorted(html_files):
        fpath = os.path.join(out_dir, fname)
        with open(fpath, 'rb') as f:
            content = base64.b64encode(f.read()).decode()

        encoded_name = quote(fname)
        url = f'{api_base}/{encoded_name}'
        payload = {
            'message': f'Update {fname} ({date_str})',
            'content': content,
        }
        if fname in existing:
            payload['sha'] = existing[fname]

        r = requests.put(url, headers=headers, json=payload)
        if r.status_code in (200, 201):
            print(f"  ✓ {fname}")
        elif r.status_code in (409, 422):
            # SHAが古いか未取得 → 個別に最新SHAを取得してリトライ
            r2 = requests.get(url, headers=headers)
            if r2.status_code == 200:
                current_sha = r2.json().get('sha')
                if current_sha:
                    payload['sha'] = current_sha
                    r3 = requests.put(url, headers=headers, json=payload)
                    if r3.status_code in (200, 201):
                        print(f"  ✓ {fname} (再試行)")
                    else:
                        try:
                            msg = r3.json().get('message', '')
                        except Exception:
                            msg = r3.text[:100]
                        print(f"  ✗ {fname}: {r3.status_code} {msg}")
                    time.sleep(1)
                else:
                    print(f"  ✗ {fname}: SHA取得失敗")
            else:
                try:
                    msg = r.json().get('message', '')
                except Exception:
                    msg = r.text[:100]
                print(f"  ✗ {fname}: {r.status_code} {msg}")
        else:
            try:
                msg = r.json().get('message', '')
            except Exception:
                msg = r.text[:100]
            print(f"  ✗ {fname}: {r.status_code} {msg}")
        time.sleep(1)

    if cleanup:
        print(f"\n  旧フォーマット・重複ファイルの削除中...")
        uploaded_basenames = set(html_files)
        for fname, sha in existing.items():
            if fname.startswith('scatter_') and fname.endswith('.html'):
                should_delete = False
                if not re.search(r'_(芝|ダ|障)\d+m\.html$', fname):
                    should_delete = True
                elif fname not in uploaded_basenames:
                    m = re.match(r'scatter_(\d{8})_([\u4e00-\u9fff]+)(\d{2}R)', fname)
                    if m:
                        prefix = f'scatter_{m.group(1)}_{m.group(2)}{m.group(3)}'
                        if any(ub.startswith(prefix) for ub in uploaded_basenames):
                            should_delete = True
                if should_delete:
                    encoded_name = quote(fname)
                    url = f'{api_base}/{encoded_name}'
                    payload = {'message': f'Cleanup: {fname}', 'sha': sha}
                    r = requests.delete(url, headers=headers, json=payload)
                    if r.status_code == 200:
                        print(f"  🗑 {fname}")
                    time.sleep(1)

    r = requests.get(api_base, headers=headers)
    all_files = {}
    if r.status_code == 200:
        for item in r.json():
            all_files[item['name']] = item['sha']

    all_scatter = sorted([f for f in all_files.keys()
                          if f.startswith('scatter_') and f.endswith('.html')],
                         reverse=True)
    date_groups = {}
    for fname in all_scatter:
        m = re.match(r'scatter_(\d{8})_(.+)\.html', fname)
        if m:
            d = m.group(1)
            _wdays = ['月', '火', '水', '木', '金', '土', '日']
            _ddt = datetime(int(d[:4]), int(d[4:6]), int(d[6:]))
            d_fmt = f"{int(d[4:6])}/{int(d[6:])}（{_wdays[_ddt.weekday()]}）"
            date_groups.setdefault(d_fmt, []).append(fname)
        else:
            date_groups.setdefault('その他', []).append(fname)

    index_html = '''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>クッション値×含水率 散布図</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body { font-family:-apple-system,BlinkMacSystemFont,'Noto Sans JP',sans-serif; background:linear-gradient(160deg,#1e3a72 0%,#162d58 50%,#1a3268 100%); min-height:100vh; color:#e8f0ff; }
.content { padding-top:10px; }
.global-header { position:sticky; top:0; z-index:100; padding:12px 18px; border-bottom:1px solid rgba(255,255,255,0.12); display:flex; align-items:center; gap:12px; background:linear-gradient(180deg,rgba(255,255,255,0.10) 0%,rgba(255,255,255,0.02) 100%),#2d4a68; box-shadow:inset 0 1px 0 rgba(255,255,255,0.22),0 4px 20px rgba(0,0,0,0.5); }
.global-header-text { flex:1; }
.global-header h1 { font-size:18px; font-weight:900; letter-spacing:-0.5px; color:#fff; }
.global-header .sub { font-size:11px; color:#a8c8e8; margin-top:2px; }
.admin-btn { display:inline-flex; align-items:center; gap:6px; background:linear-gradient(180deg,rgba(255,255,255,0.15) 0%,rgba(255,255,255,0.05) 100%),#3a6d9a; border:1px solid rgba(255,255,255,0.25); border-top:1px solid rgba(255,255,255,0.4); color:#fff; font-size:12px; font-weight:700; padding:7px 14px; border-radius:8px; text-decoration:none; transition:all 0.15s; white-space:nowrap; flex-shrink:0; box-shadow:inset 0 1px 0 rgba(255,255,255,0.2),0 3px 10px rgba(0,0,0,0.3); }
.admin-btn:hover { background:linear-gradient(180deg,rgba(255,255,255,0.22) 0%,rgba(255,255,255,0.08) 100%),#3a6d9a; border-color:rgba(255,255,255,0.5); }
.date-section { margin-bottom:8px; padding:0 10px; }
.date-header { font-size:15px; font-weight:800; padding:14px 18px; background:linear-gradient(180deg,rgba(255,255,255,0.12) 0%,rgba(255,255,255,0.03) 60%,rgba(0,0,0,0.08) 100%),#1a5276; color:#ddeeff; cursor:pointer; display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:12px; border:1px solid rgba(255,255,255,0.15); border-top:1px solid rgba(255,255,255,0.30); border-radius:12px; box-shadow:inset 0 1px 0 rgba(255,255,255,0.22),0 6px 20px rgba(0,0,0,0.45); transition:all 0.2s; }
.date-header:hover,.date-header:active { background:linear-gradient(180deg,rgba(255,255,255,0.18) 0%,rgba(255,255,255,0.06) 100%),#1a5276; border-color:rgba(255,255,255,0.35); box-shadow:inset 0 1px 0 rgba(255,255,255,0.3),0 8px 28px rgba(0,0,0,0.5); }
.date-header .toggle { font-size:11px; color:#a8c8e8; transition:transform 0.2s; }
.date-header.open .toggle { transform:rotate(180deg); }
.date-left { white-space:nowrap; }
.graded-center { font-size:clamp(8px,2vw,13px); font-weight:800; color:#e8f4ff; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; line-height:1.8; }
.hdr-badge { font-size:clamp(7px,1.6vw,10px); font-weight:900; padding:1px 4px; border-radius:3px; vertical-align:middle; display:inline-block; line-height:1.4; }
.hdr-g1 { background:rgba(239,68,68,0.3); color:#fca5a5; border:1px solid rgba(239,68,68,0.6); }
.hdr-g2 { background:rgba(168,85,247,0.3); color:#d8b4fe; border:1px solid rgba(168,85,247,0.6); }
.hdr-g3 { background:rgba(58,109,154,0.5); color:#bdd8f0; border:1px solid rgba(58,109,154,0.8); }
.race-list { display:none; padding:12px 14px; overflow-x:auto; background:rgba(22,45,88,0.6); }
.race-list.open { display:flex; gap:10px; align-items:flex-start; }
.venue-col { flex:0 0 auto; min-width:270px; background:linear-gradient(180deg,rgba(255,255,255,0.07) 0%,rgba(255,255,255,0.01) 100%),#2d4a68; border:1px solid rgba(255,255,255,0.14); border-top:1px solid rgba(255,255,255,0.25); border-radius:10px; overflow:hidden; box-shadow:inset 0 1px 0 rgba(255,255,255,0.15),0 4px 16px rgba(0,0,0,0.35); }
.venue-head { padding:8px 14px; background:linear-gradient(180deg,rgba(255,255,255,0.10) 0%,rgba(255,255,255,0.02) 100%),#3a6d9a; border-bottom:1px solid rgba(255,255,255,0.12); box-shadow:inset 0 1px 0 rgba(255,255,255,0.2); }
.venue-title { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; }
.venue-head h3 { font-size:14px; font-weight:900; color:#fff; }
.cv-inline { font-size:10px; color:#c8e0f8; font-weight:600; }
.weather-row { display:flex; gap:8px; margin-top:5px; }
.weather-slot { display:flex; flex-direction:column; align-items:center; gap:1px; }
.weather-icon { font-size:16px; line-height:1; }
.weather-label { font-size:8px; color:#7aa8c8; font-weight:600; }
a { display:flex; align-items:center; gap:8px; padding:9px 14px; color:#ddeeff; text-decoration:none; font-size:12px; font-weight:700; border-bottom:1px solid rgba(255,255,255,0.07); transition:background 0.12s; }
a:last-child { border-bottom:none; }
a:hover, a:active { background:rgba(255,255,255,0.08); }
.rinfo { display:flex; flex-direction:column; min-width:44px; flex-shrink:0; }
.rnum { font-size:13px; font-weight:800; color:#c8e0f8; }
.rtime { display:flex; align-items:center; gap:4px; min-height:13px; }
.lamp { width:6px; height:6px; border-radius:50%; flex-shrink:0; margin-top:1px; }
.lamp-green { background:#22c55e; box-shadow:0 0 6px #22c55e; }
.lamp-red { background:#ef4444; box-shadow:0 0 6px #ef4444; }
.lamp-gray { background:#4a6888; }
.rtime-text { font-size:9px; color:#7aa8c8; font-weight:600; }
.rname { flex:1; white-space:nowrap; font-size:14px; }
.surf-badge { font-size:9px; font-weight:800; padding:2px 6px; border-radius:4px; flex-shrink:0; margin-top:1px; }
.surf-turf { background:rgba(34,197,94,0.22); color:#4ade80; border:1px solid rgba(34,197,94,0.45); }
.surf-dirt { background:rgba(245,158,11,0.22); color:#fbbf24; border:1px solid rgba(245,158,11,0.45); }
.dist { font-size:11px; color:#b0cce8; font-weight:600; flex-shrink:0; }
.grade-g1 { font-size:9px; font-weight:900; padding:1px 5px; border-radius:4px; flex-shrink:0; background:rgba(239,68,68,0.3); color:#fca5a5; border:1px solid rgba(239,68,68,0.6); }
.grade-g2 { font-size:9px; font-weight:900; padding:1px 5px; border-radius:4px; flex-shrink:0; background:rgba(168,85,247,0.3); color:#d8b4fe; border:1px solid rgba(168,85,247,0.6); }
.grade-g3 { font-size:9px; font-weight:900; padding:1px 5px; border-radius:4px; flex-shrink:0; background:rgba(58,109,154,0.5); color:#bdd8f0; border:1px solid rgba(58,109,154,0.8); }
.week-badge { font-size:9px; font-weight:800; padding:2px 7px; border-radius:10px; margin-left:6px; background:rgba(34,197,94,0.22); color:#4ade80; border:1px solid rgba(34,197,94,0.45); }
.week-badge-last { background:rgba(100,116,139,0.2); color:#94a3b8; border:1px solid rgba(100,116,139,0.3); }
</style>
</head>
<body>
<div class="global-header">
  <div class="global-header-text">
    <h1>クッション値×含水率 散布図</h1>
    <div class="sub">日付をタップで展開 → レースを選択</div>
  </div>
  <a class="admin-btn" href="http://localhost:5000" target="_blank">⚙ 管理</a>
</div>
<div class="content">
'''
    from datetime import datetime as _dt
    _today = _dt.now()
    _today_week = _today.isocalendar()[1]
    _today_year = _today.isocalendar()[0]

    _today_str = _today.strftime('%Y%m%d')
    def _date_sort_key(d_fmt_key):
        for fn in date_groups[d_fmt_key]:
            dm = re.match(r'scatter_(\d{8})_', fn)
            if dm:
                d = dm.group(1)
                if d >= _today_str:
                    return (0, d)
                else:
                    return (1, str(99999999 - int(d)))
        return (2, d_fmt_key)
    date_keys = sorted(date_groups.keys(), key=_date_sort_key)
    for idx, d_fmt in enumerate(date_keys):
        files_in_date = sorted(date_groups[d_fmt])
        open_class = ''

        # 日付文字列を取得（ファイル名から）
        raw_date_hdr = ''
        for fn in files_in_date:
            dm = re.match(r'scatter_(\d{8})_', fn)
            if dm:
                raw_date_hdr = dm.group(1)
                break

        # 今週バッジ（今週のみ表示）
        week_badge_html = ''
        if raw_date_hdr:
            rd = _dt(int(raw_date_hdr[:4]), int(raw_date_hdr[4:6]), int(raw_date_hdr[6:]))
            rd_iso = rd.isocalendar()
            if rd_iso[0] == _today_year and rd_iso[1] == _today_week:
                week_badge_html = '<span class="week-badge">今週</span>'

        # 重賞レース名取得（キャッシュまたは新規取得）
        grades_for_date = all_grades.get(raw_date_hdr)
        if grades_for_date is None:
            grades_for_date = fetch_grades_for_date(raw_date_hdr)
            all_grades[raw_date_hdr] = grades_for_date

        # 例: 中山11R　GⅠ　皐月賞　/　福島11R　GⅢ　福島牝馬S
        roman = {'G1': 'GⅠ', 'G2': 'GⅡ', 'G3': 'GⅢ'}
        graded_parts = []
        for key, (rn, gr) in list(grades_for_date.items())[:3]:
            venue_r, rnum_r = key.split('_')
            gr_roman = roman.get(gr, gr)
            gcls = 'hdr-g1' if gr == 'G1' else ('hdr-g2' if gr == 'G2' else 'hdr-g3')
            graded_parts.append(
                f'{venue_r}{int(rnum_r)}R\u2009<span class="hdr-badge {gcls}">{gr_roman}</span>\u2009{rn}'
            )
        graded_html = (
            '<span class="graded-in-date">' +
            '\u3000/\u3000'.join(graded_parts) +
            '</span>'
        ) if graded_parts else ''

        index_html += f'<div class="date-section">'
        index_html += (
            f'<div class="date-header{open_class}" onclick="toggleDate(this)">'
            f'<span class="date-left">{d_fmt}{week_badge_html}</span>'
            f'<span class="graded-center">{graded_html}</span>'
            f'<span class="toggle">▼</span></div>\n'
        )
        index_html += f'<div class="race-list{open_class}">\n'

        venue_groups = {}
        for fname in files_in_date:
            stripped = re.sub(r'^scatter_\d{8}_', '', fname)
            vm = re.match(r'([\u4e00-\u9fff]+)\d', stripped)
            venue_name = vm.group(1) if vm else 'その他'
            venue_groups.setdefault(venue_name, []).append(fname)

        venue_order = ['東京', '京都', '小倉', '中山', '阪神', '中京', '新潟', '福島', '函館', '札幌']
        sorted_venues = sorted(venue_groups.keys(), key=lambda v: venue_order.index(v) if v in venue_order else 99)

        # この日付の全会場の天気をまとめて取得
        raw_date_for_group = ''
        for vf in venue_groups.values():
            dm2 = re.match(r'scatter_(\d{8})_', sorted(vf)[0])
            if dm2:
                raw_date_for_group = dm2.group(1)
                break
        weather_by_venue = {}
        try:
            weather_by_venue = fetch_venue_weather(list(sorted_venues), raw_date_for_group)
        except Exception:
            pass

        for venue_name in sorted_venues:
            vfiles = sorted(venue_groups[venue_name])
            raw_date = ''
            cv_inline = ''
            if vfiles:
                dm = re.match(r'scatter_(\d{8})_', vfiles[0])
                if dm:
                    raw_date = dm.group(1)
                    db_key = f"{raw_date[:4]}/{raw_date[4:6]}/{raw_date[6:]}_{venue_name}"
                    if db_key in cushion_db:
                        e = cushion_db[db_key]
                        cv_inline = f'CV {e.get("cushion","?")} 芝{e.get("turf_goal","?")}% ダ{e.get("dirt_goal","?")}%'

            # 9/12/15時の天気アイコン
            wemojis = weather_by_venue.get(venue_name, [])
            weather_row_html = ''
            if wemojis:
                slots = zip(['9時', '12時', '15時'], wemojis)
                weather_row_html = '<div class="weather-row">' + ''.join(
                    f'<div class="weather-slot"><span class="weather-icon">{em}</span><span class="weather-label">{lbl}</span></div>'
                    for lbl, em in slots
                ) + '</div>'

            cv_html = f'<span class="cv-inline">{cv_inline}</span>' if cv_inline else ''
            index_html += (
                f'<div class="venue-col"><div class="venue-head">'
                f'<div class="venue-title"><h3>{venue_name}</h3>{cv_html}</div>'
                f'{weather_row_html}</div>\n'
            )

            # この日のstart_timesを取得
            st_map = all_start_times.get(raw_date, {})

            for fname in vfiles:
                pm = re.match(r'scatter_\d{8}_' + re.escape(venue_name) + r'(\d{2})R_(.+)_(芝|ダ|障)(\d+)m', fname)
                if pm:
                    rnum = int(pm.group(1))
                    rname = pm.group(2).replace('_', ' ')
                    surf = pm.group(3)
                    dist = pm.group(4)
                    badge_cls = 'surf-turf' if surf == '芝' else 'surf-dirt'
                    st_key = f'{venue_name}_{rnum:02d}'
                    start_time = st_map.get(st_key, '')
                    lamp_html = f'<span class="lamp" data-time="{start_time}"></span>'
                    time_html = f'<span class="rtime-text">{start_time}</span>' if start_time else ''
                    # 重賞バッジ
                    grade_html = ''
                    if st_key in grades_for_date:
                        _, gr = grades_for_date[st_key]
                        gcls = 'grade-g1' if gr == 'G1' else ('grade-g2' if gr == 'G2' else 'grade-g3')
                        grade_html = f'<span class="{gcls}">{gr}</span>'
                    index_html += (
                        f'<a href="{fname}" data-venue="{venue_name}" data-rnum="{rnum}" data-date="{raw_date}">'
                        f'<span class="rinfo"><span class="rnum">{rnum}R</span>'
                        f'<span class="rtime">{lamp_html}{time_html}</span></span>'
                        f'<span class="rname">{rname}</span>'
                        f'{grade_html}'
                        f'<span class="surf-badge {badge_cls}">{surf}</span>'
                        f'<span class="dist">{dist}m</span></a>\n'
                    )
                else:
                    display = re.sub(r'^scatter_\d{8}_' + re.escape(venue_name), '', fname).replace('.html', '').replace('_', ' ').strip()
                    index_html += f'<a href="{fname}">{display}</a>\n'
            index_html += '</div>\n'

        index_html += '</div></div>\n'

    index_html += '''<script>
function toggleDate(el){
  el.classList.toggle('open');
  el.nextElementSibling.classList.toggle('open');
}

function todayStr(){
  var d = new Date();
  var y = d.getFullYear();
  var m = String(d.getMonth()+1).padStart(2,'0');
  var day = String(d.getDate()).padStart(2,'0');
  return y+m+day;
}

function updateLamps(){
  var now = new Date();
  var today = todayStr();
  var venueGroups = {};

  document.querySelectorAll('a[data-venue]').forEach(function(a){
    var lamp = a.querySelector('.lamp');
    if(!lamp) return;
    var raceDate = a.getAttribute('data-date') || '';
    // 当日以外は全てグレー
    if(raceDate !== today){
      lamp.className = 'lamp lamp-gray';
      return;
    }
    var t = lamp.getAttribute('data-time');
    if(!t) return;
    var parts = t.split(':');
    var race = new Date(now); race.setHours(+parts[0], +parts[1], 0, 0);
    var venue = a.getAttribute('data-venue');
    if(!venueGroups[venue]) venueGroups[venue] = [];
    venueGroups[venue].push({lamp:lamp, race:race, diff:Math.abs(race-now)});
  });

  Object.keys(venueGroups).forEach(function(venue){
    var items = venueGroups[venue];
    var minDiff = Math.min.apply(null, items.map(function(x){return x.diff;}));
    items.forEach(function(item){
      item.lamp.className = 'lamp';
      if(item.diff === minDiff){
        item.lamp.classList.add('lamp-red');
      } else if(item.race > now){
        item.lamp.classList.add('lamp-green');
      } else {
        item.lamp.classList.add('lamp-gray');
      }
    });
  });
}

updateLamps();
setInterval(updateLamps, 30000);
</script>
</div></body></html>'''

    encoded_name = quote('index.html')
    url = f'{api_base}/{encoded_name}'
    payload = {
        'message': f'Update index ({date_str})',
        'content': base64.b64encode(index_html.encode('utf-8')).decode(),
    }
    if 'index.html' in all_files:
        payload['sha'] = all_files['index.html']
    r = requests.put(url, headers=headers, json=payload)
    if r.status_code in (200, 201):
        print(f"  ✓ index.html (日付別リンク付き)")

    pages_url = f'https://{repo.split("/")[0]}.github.io/{repo.split("/")[1]}/'
    print(f"\n  デプロイ完了！")
    print(f"  📱 スマホでアクセス: {pages_url}")


if __name__ == '__main__':
    main()
