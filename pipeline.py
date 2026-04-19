#!/usr/bin/env python3
"""
競馬クッション値×含水率 散布図 一括生成パイプライン v2

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
import asyncio
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date as _date
from urllib.parse import quote

# JRA公式スクレイパー（keiba-app）をインポート
_JRA_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'keiba-app', 'backend', 'scrapers')
if os.path.isdir(_JRA_APP) and _JRA_APP not in sys.path:
    sys.path.insert(0, _JRA_APP)
try:
    from jra_entries import JRAPlaywrightScraper
    _JRA_AVAILABLE = True
except ImportError:
    _JRA_AVAILABLE = False

# ===== 設定 =====
CUSHION_DB_PATH = os.path.join(os.path.dirname(__file__), 'cushion_db_full.json')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
CACHE_DIR = os.path.join(os.path.dirname(__file__), 'cache')

VENUE_CODES = {
    '01': '札幌', '02': '函館', '03': '福島', '04': '新潟',
    '05': '東京', '06': '中山', '07': '中京', '08': '京都',
    '09': '阪神', '10': '小倉'
}


# ===== JRA公式スクレイパー統合 =====

def fetch_jra_races(date_str):
    """
    JRA公式JRADB（Playwright）からレース一覧+馬データを取得し、
    pipeline形式で返す。
    Returns: (races_list, race_data_map)
      races_list: [{race_id, venue, race_num, race_name, surface, distance, start_time}]
      race_data_map: {race_id: {race_info, horses, horse_nums}}
    """
    if not _JRA_AVAILABLE:
        return [], {}

    def _convert_histories(histories):
        converted = []
        for hr in histories:
            hdate = hr.get('race_date')
            if isinstance(hdate, _date):
                hdate_str = hdate.strftime('%Y/%m/%d')
            else:
                hdate_str = str(hdate)
            hsurf = '芝' if hr.get('course_type') == 'turf' else 'ダ'
            converted.append({
                'date':       hdate_str,
                'venue':      hr.get('venue', ''),
                'surface':    hsurf,
                'distance':   hr.get('distance', 0),
                'race_name':  hr.get('race_name', ''),
                'result':     hr.get('result_rank'),
                'num_horses': hr.get('num_horses', ''),
                'time_diff':  '',
                'passage':    '',
                'pace':       '',
                'agari':      '',
                'winner':     '',
            })
        return converted

    async def _run():
        from jra_entries import _parse_horse_history_table, BASE_URL, JRADB_U
        target = _date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]))
        today  = _date.today()
        is_past = target < today

        async with JRAPlaywrightScraper() as sc:
            if is_past:
                cnames = await sc.get_past_race_cnames(target)
            else:
                cnames = await sc.get_shutuba_cnames(target)

            if not cnames:
                return [], {}

            races_list = []
            race_meta  = {}

            # Phase 1: レース情報取得（馬名・CNAMEを収集）
            for rc in sorted(cnames, key=lambda x: (x.get('venue') or '', x.get('race_num', 0))):
                cname    = rc['cname']
                venue    = rc.get('venue') or '?'
                race_num = rc.get('race_num', 0)
                fake_id  = f"jra_{date_str}_{venue}_{race_num:02d}"

                try:
                    rdata = await (sc.parse_spr10_race(cname, venue, race_num) if is_past
                                   else sc.parse_dde010_race(cname, venue, race_num))
                except Exception as e:
                    print(f"  レース取得エラー {venue}{race_num}R: {e}")
                    continue

                if not rdata:
                    continue

                surface = '芝' if rdata.get('course_type') == 'turf' else 'ダ'
                dist    = rdata.get('distance', 0)
                rname   = rdata.get('race_name', '')

                races_list.append({
                    'race_id':    fake_id,
                    'venue':      venue,
                    'race_num':   race_num,
                    'race_name':  rname,
                    'surface':    surface,
                    'distance':   dist,
                    'start_time': rdata.get('start_time', ''),
                    'text':       f"{race_num}R{rname}{surface}{dist}m",
                    '_cname':     cname,
                    '_is_past':   is_past,
                })

                horse_nums = {}
                raw_horses = []
                for h in rdata.get('horses', []):
                    hname = h.get('horse_name', '')
                    if not hname:
                        continue
                    horse_nums[hname] = str(h.get('horse_num', ''))
                    raw_horses.append({'name': hname, 'hcname': h.get('history_cname')})

                race_meta[fake_id] = {
                    'race_info':  {'race_id': fake_id, 'race_name': rname,
                                   'venue': venue, 'surface': surface, 'distance': dist},
                    'horse_nums': horse_nums,
                    'raw_horses': raw_horses,
                }

            # Phase 2: Playwright で馬歴を並列取得（同一ブラウザの複数ページ）
            all_horses = [(fid, h['name'], h['hcname'])
                          for fid, meta in race_meta.items()
                          for h in meta['raw_horses'] if h['hcname']]
            total = len(all_horses)
            print(f"  馬歴並列取得: {total}頭 (最大6並列)", flush=True)

            sem = asyncio.Semaphore(6)
            done_count = 0
            hist_results = {}

            async def fetch_one(fid, hname, hcname):
                nonlocal done_count
                async with sem:
                    try:
                        url  = f"{BASE_URL}{JRADB_U}?CNAME={hcname}"
                        page = await sc._browser.new_page()
                        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        html = await page.content()
                        await page.close()
                        raw  = _parse_horse_history_table(html, hname)
                    except Exception:
                        raw = []
                    done_count += 1
                    converted = _convert_histories(raw[:10])
                    print(f"    [{done_count}/{total}] {hname}: {len(converted)}走", flush=True)
                    hist_results[(fid, hname)] = converted

            await asyncio.gather(*[fetch_one(fid, hname, hcname)
                                   for fid, hname, hcname in all_horses])

            # Phase 3: まとめ
            race_data_map = {}
            for fid, meta in race_meta.items():
                horses_dict = {h['name']: hist_results.get((fid, h['name']), [])
                               for h in meta['raw_horses']}
                race_data_map[fid] = {
                    'race_info':  meta['race_info'],
                    'horses':     horses_dict,
                    'horse_nums': meta['horse_nums'],
                }

            return races_list, race_data_map

    try:
        return asyncio.run(_run())
    except Exception as e:
        print(f"  JRAスクレイパーエラー: {e}")
        return [], {}


# ===== Step 1: JRA ライブデータ取得 =====
def fetch_jra_live():
    """JRA公式からクッション値・含水率をリアルタイム取得"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    result = {}

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

    return result


# ===== JRA重賞グレード取得 =====
def fetch_jra_graded_races():
    """JRA今週の重賞レースページからレース名→グレードのdictを返す"""
    try:
        r = requests.get('https://www.jra.go.jp/keiba/thisweek/',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(r.content, 'html.parser', from_encoding='shift_jis')
        graded = {}
        grade_re = re.compile(r'[（(]([GＧJ・]*[ⅠⅡⅢ1-3]+)[）)]')
        for h3 in soup.find_all('h3'):
            text = h3.get_text(strip=True)
            gm = grade_re.search(text)
            if not gm:
                continue
            g = gm.group(1).replace('Ｇ', 'G').replace('Ⅰ', '1').replace('Ⅱ', '2').replace('Ⅲ', '3')
            g = re.sub(r'^J.*G', 'G', g)  # J・GⅠ → G1
            race_name = grade_re.sub('', text).strip()
            norm = _norm_race(race_name)
            graded[norm] = g
        return graded
    except Exception as e:
        print(f"  JRA重賞ページ取得失敗: {e}")
        return {}


def _norm_race(name):
    """レース名正規化（マッチング用）"""
    n = re.sub(r'[　 ・･]', '', name)
    n = re.sub(r'(ステークス|スタークス|カップ|賞典|記念)$', '', n)
    n = re.sub(r'[SsCc]$', '', n)  # 略称 "S" "C" 除去
    return n


def match_grade(race_name, jra_graded):
    norm = _norm_race(race_name)
    if norm in jra_graded:
        return jra_graded[norm]
    # 部分一致（スポンサー名付きや略称対応）
    for jra_norm, grade in jra_graded.items():
        if norm and jra_norm and (norm in jra_norm or jra_norm in norm):
            return grade
    return ''


# ===== Step 2: レース一覧取得（JRA公式のみ） =====
# netkeibaへのアクセスは行わない


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


# ===== 気象類似条件による予測範囲算出 =====
_VENUE_JA_TO_EN = {
    '札幌': 'sapporo', '函館': 'hakodate', '福島': 'fukushima', '新潟': 'niigata',
    '東京': 'tokyo', '中山': 'nakayama', '中京': 'chukyo', '京都': 'kyoto',
    '阪神': 'hanshin', '小倉': 'kokura',
}

def compute_weather_range(venue, surface, date_str):
    """同会場・同路面・類似気象条件の過去データからクッション値・含水率の分布を返す"""
    import csv as _csv
    obs_path = os.path.join(os.path.dirname(__file__), 'data', 'observations.csv')
    if not os.path.exists(obs_path):
        return None

    with open(obs_path, encoding='utf-8') as f:
        all_rows = list(_csv.DictReader(f))

    # venue: 日本語 → 英語キーに変換（CSVのvenueカラムは英語）
    venue_en = _VENUE_JA_TO_EN.get(venue, venue)
    surface_key = 'turf' if surface != 'ダ' else 'dirt'

    # date_str: YYYYMMDD → YYYY-MM-DD（CSVのdate形式に合わせる）
    if len(date_str) == 8 and '-' not in date_str:
        date_csv = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    else:
        date_csv = date_str

    try:
        target_month = int(date_csv[5:7])
    except Exception:
        return None

    # 対象日の気象データ
    target_wx = {}
    for r in all_rows:
        if r.get('date') == date_csv and r.get('venue') == venue_en and r.get('surface') == surface_key:
            target_wx = r
            break

    def month_diff(m1, m2):
        d = abs(m1 - m2)
        return min(d, 12 - d)

    def rain_cat(v):
        try:
            v = float(v or 0)
        except Exception:
            v = 0
        return 0 if v < 1 else (1 if v < 5 else (2 if v < 20 else 3))

    # ダートの場合は同日・同会場の芝クッション値を使う（クッション値は芝のみ計測）
    turf_cv_by_date = {}
    if surface_key == 'dirt':
        for r in all_rows:
            if r.get('venue') == venue_en and r.get('surface') == 'turf':
                try:
                    turf_cv_by_date[r['date']] = float(r['cushion_value'])
                except (ValueError, TypeError):
                    pass

    candidates = []
    for r in all_rows:
        if r.get('venue') != venue_en or r.get('surface') != surface_key:
            continue
        if r.get('date') == date_csv:
            continue
        try:
            if surface_key == 'dirt':
                cv = turf_cv_by_date.get(r['date'])
                if cv is None:
                    continue
            else:
                cv = float(r['cushion_value'])
            mo = float(r['moisture_rate'])
        except (ValueError, TypeError):
            continue
        if cv <= 0 or mo <= 0:
            continue
        try:
            row_month = int(r['date'][5:7])
        except Exception:
            continue
        if month_diff(row_month, target_month) > 2:
            continue

        # 気象条件フィルター
        if target_wx:
            try:
                t_temp = float(target_wx.get('temperature_avg') or '')
                r_temp = float(r.get('temperature_avg') or '')
                if abs(t_temp - r_temp) > 6:
                    continue
            except (ValueError, TypeError):
                pass
            try:
                r_rain_raw = r.get('rainfall_24h', '')
                if r_rain_raw:  # 空欄はスキップ（不明として扱う）
                    t_rain = rain_cat(target_wx.get('rainfall_24h', '0'))
                    r_rain = rain_cat(r_rain_raw)
                    if t_rain != r_rain:
                        continue
            except (ValueError, TypeError):
                pass

        candidates.append((cv, mo))

    if len(candidates) < 5:
        return None

    cvs = sorted(c[0] for c in candidates)
    mos = sorted(c[1] for c in candidates)

    def pct(data, p):
        n = len(data)
        idx = (n - 1) * p / 100
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        return round(data[lo] + (data[hi] - data[lo]) * (idx - lo), 2)

    return {
        'cv_p10':  pct(cvs, 10), 'cv_p25':  pct(cvs, 25),
        'cv_p75':  pct(cvs, 75), 'cv_p90':  pct(cvs, 90),
        'mo_p10':  pct(mos, 10), 'mo_p25':  pct(mos, 25),
        'mo_p75':  pct(mos, 75), 'mo_p90':  pct(mos, 90),
        'count': len(candidates),
    }


# ===== Step 5: 散布図HTML生成 (ダークテーマ v2) =====
def generate_scatter_html(race_data, target_cushion, target_moisture, output_path, date_label='', race_num=0, race_date='', weather_range=None):
    """散布図HTMLを生成（ダークテーマ・モバイル対応）"""
    race_info = race_data['race_info']
    venue = race_info['venue']
    race_name = race_info['race_name']
    surface = race_info['surface']
    distance = race_info['distance']
    start_time = race_info.get('start_time', '')

    horse_nums = race_data.get('horse_nums', {})
    def get_waku_color(num_horses, umaban):
        waku_hex = ['#FFFFFF','#222222','#D83A3A','#2E6FD1','#F5C518','#2A8A4A','#E88527','#F3A3BD']
        n = num_horses
        if n <= 8:
            arr = [1]*n + [0]*(8-n)
        elif n <= 16:
            arr = [1]*8
            for i in range(n-8): arr[7-i] = 2
        elif n == 17:
            arr = [2,2,2,2,2,2,2,3]
        else:
            arr = [2,2,2,2,2,2,3,3]
        uma = 1
        for wi, cnt in enumerate(arr):
            for _ in range(cnt):
                if uma == umaban: return waku_hex[wi]
                uma += 1
        return '#888888'

    js_horses = []
    for horse_name, races in race_data['horses'].items():
        js_races = []
        for r in races[:10]:
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
        hnum_str = horse_nums.get(horse_name, '')
        try:
            hnum_int = int(hnum_str)
        except (ValueError, TypeError):
            hnum_int = 999
        js_horses.append({
            'name': horse_name,
            'horse_num': hnum_str,
            'races': js_races,
            '_sort': hnum_int,
        })

    js_horses.sort(key=lambda h: h['_sort'])
    total_horses = len(js_horses)
    for h in js_horses:
        try:
            h['waku_color'] = get_waku_color(total_horses, int(h['horse_num']))
        except (ValueError, TypeError):
            h['waku_color'] = '#888888'
        del h['_sort']

    horses_json = json.dumps(js_horses, ensure_ascii=False)
    weather_range_json = json.dumps(weather_range, ensure_ascii=False) if weather_range else 'null'
    race_id_str = race_info.get('race_id', '')
    surface_label = '芝' if surface == '芝' else 'ダート'
    surf_class = 'turf' if surface == '芝' else 'dirt'
    surf_lbl = '芝' if surface == '芝' else 'ダ'
    color_same = f'同距離{surface_label}'
    color_diff = f'他距離{surface_label}'
    color_other = 'ダート' if surface == '芝' else '芝レース'

    # AI読み取り用構造化データ
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
                '3着以内': '○' if r['good'] else '',
            })
        structured_data['出走馬'].append(horse_entry)
    structured_json = json.dumps(structured_data, ensure_ascii=False, indent=2)

    ai_text_lines = []
    ai_text_lines.append(f"【{date_label} {venue}{race_num}R {race_name} {surface_label}{distance}m】")
    ai_text_lines.append(f"当日条件: クッション値={target_cushion} 含水率={target_moisture}%")
    ai_text_lines.append("")
    for h in js_horses:
        ai_text_lines.append(f"■ {h['name']}（過去{len(h['races'])}走）")
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
<title>{venue}{race_num}R {race_name} - クッション値×含水率</title>
<style>
:root {{
  --bg-start: #0a1e4a; --bg-mid: #0f2d6b; --bg-end: #0a1a3d;
  --bg-card: rgba(255,255,255,0.07); --bg-card2: rgba(255,255,255,0.04);
  --border: rgba(100,160,255,0.2); --border2: rgba(100,160,255,0.4);
  --text: #e8f0ff; --text-sub: #a8c4e8; --text-muted: #7ea8d8;
  --accent-cv: #f59e0b; --accent-moist: #38bdf8;
  --red: #ef4444; --blue: #3b82f6; --green: #22c55e;
  --yellow: #eab308; --gray: #64748b;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ height: 100%; overflow: hidden; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Noto Sans JP', 'Hiragino Sans', sans-serif;
  background: linear-gradient(160deg, var(--bg-start) 0%, var(--bg-mid) 50%, var(--bg-end) 100%) fixed;
  color: var(--text); display: flex; flex-direction: column;
  -webkit-font-smoothing: antialiased;
}}

/* ── Header ── */
.header {{
  background: linear-gradient(135deg, rgba(30,80,160,0.55), rgba(10,45,107,0.55));
  border-bottom: 1px solid var(--border2);
  padding: 10px 14px; flex-shrink: 0;
  box-shadow: 0 2px 16px rgba(0,10,60,0.5);
  backdrop-filter: blur(8px);
}}
.header-row {{ display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
.header h1 {{
  font-size: 13px; font-weight: 900; letter-spacing: -0.3px;
  background: linear-gradient(90deg, #ffffff, #7eb8f7);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  line-height: 1.3; white-space: nowrap;
}}
.header .sub {{ display: none; }}
.header-badges {{ display: none; }}
.header-time {{ font-size: 13px; color: var(--text-muted); font-weight: 700; flex-shrink: 0; }}
@media (max-width: 500px) {{
  .header-row {{ gap: 4px; }}
  .header h1 {{ font-size: 10px; letter-spacing: -0.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; }}
  .badge {{ font-size: 10px; padding: 2px 5px; min-width: 60px; }}
  .header-time {{ font-size: 10px; }}
  .sbadge {{ font-size: 10px; padding: 1px 4px; }}
}}
.sbadge {{
  font-size: 13px; font-weight: 900; padding: 2px 6px; border-radius: 6px; flex-shrink: 0;
}}
.sbadge.turf {{ background: rgba(34,197,94,0.2); color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }}
.sbadge.dirt {{ background: rgba(245,158,11,0.2); color: #fbbf24; border: 1px solid rgba(245,158,11,0.4); }}
.badge {{
  display: inline-flex; align-items: center; justify-content: center; gap: 5px;
  background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.25);
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  padding: 3px 8px; border-radius: 6px; min-width: 78px; flex-shrink: 0;
  font-size: 13px; font-weight: 700; font-family: monospace; color: #ffffff;
}}
.badge b {{ color: #ffffff; }}

/* ── Layout ── */
.main {{ display: flex; flex: 1; overflow: hidden; flex-direction: column; }}
@media (min-width: 700px) {{ .main {{ flex-direction: row; }} }}

/* ── Chart ── */
.chart-wrap {{
  position: relative; flex-shrink: 0;
  height: 42vh; min-height: 240px;
  background: rgba(5,15,45,0.5);
}}
@media (min-width: 700px) {{
  .chart-wrap {{ flex: 1; height: auto; min-height: unset; }}
}}
canvas {{ display: block; width: 100% !important; height: 100% !important; touch-action: pan-y; }}

/* ── Legend ── */
.legend {{
  display: flex; gap: 10px; padding: 6px 14px;
  font-size: 10px; font-weight: 700; color: var(--text-muted);
  background: rgba(10,25,70,0.6); border-top: 1px solid var(--border);
  flex-wrap: wrap; flex-shrink: 0; backdrop-filter: blur(4px);
}}
.legend-item {{ display: flex; align-items: center; gap: 4px; }}
.ldot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}

/* ── Panel ── */
.panel {{
  background: rgba(8,20,60,0.4); overflow-y: auto; padding: 6px 6px 80px 6px;
  border-top: 1px solid var(--border);
  flex: 1;
  -webkit-overflow-scrolling: touch;
  backdrop-filter: blur(4px);
}}
@media (min-width: 700px) {{
  .panel {{
    width: 300px; border-top: none; border-left: 1px solid var(--border2);
    min-width: 260px;
  }}
}}

/* ── Horse button ── */
.horse-btn {{
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 9px 12px; margin-bottom: 3px;
  border: 1px solid var(--border); border-radius: 10px;
  background: rgba(255,255,255,0.06); cursor: pointer;
  transition: border-color 0.15s, background 0.15s, box-shadow 0.15s;
  font-size: 13px; font-weight: 700; color: var(--text);
  -webkit-tap-highlight-color: transparent; text-align: left;
}}
.horse-btn:active {{ transform: scale(0.98); }}
.horse-btn.selected {{
  border-color: var(--accent-cv); background: rgba(245,158,11,0.12);
  box-shadow: 0 0 0 2px rgba(245,158,11,0.25);
}}
.horse-btn.no-data {{ opacity: 0.45; }}
.h-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.h-num {{
  font-size: 10px; font-weight: 800; color: #fff;
  background: var(--text-muted); border-radius: 4px;
  padding: 1px 5px; min-width: 20px; text-align: center; flex-shrink: 0;
}}
.h-count {{ font-size: 10px; color: var(--text-muted); font-weight: 600; margin-left: auto; white-space: nowrap; }}
.h-odds {{ font-size: 9px; color: var(--text-muted); white-space: nowrap; flex-shrink: 0; }}
.h-odds .pop1 {{ color: #ef4444; font-weight: 900; }}
.h-odds .pop2 {{ color: #f59e0b; font-weight: 800; }}
.h-odds .pop3 {{ color: #60a5fa; font-weight: 700; }}
.h-odds .odds-low {{ color: #ef4444; font-weight: 800; }}

/* ── Rating row ── */
.rating-row {{ display: flex; gap: 4px; padding: 3px 12px 6px 22px; }}
.rating-btn {{
  width: 30px; height: 26px; border: 1.5px solid var(--border);
  border-radius: 6px; background: var(--bg-card); cursor: pointer;
  font-size: 11px; font-weight: 800; color: var(--text-muted);
  transition: all 0.12s; -webkit-tap-highlight-color: transparent;
}}
.rating-btn:active {{ transform: scale(0.9); }}
.rating-btn.rated-S {{ background: #dc2626; border-color: #dc2626; color: #fff; }}
.rating-btn.rated-A {{ background: #f59e0b; border-color: #f59e0b; color: #fff; }}
.rating-btn.rated-B {{ background: #3b82f6; border-color: #3b82f6; color: #fff; }}
.rating-btn.rated-C {{ background: #22c55e; border-color: #22c55e; color: #1e293b; }}
.rating-btn.rated-D {{ background: #64748b; border-color: #64748b; color: #fff; }}

/* ── Horse detail ── */
.horse-detail {{ display: none; padding: 4px 2px 4px 2px; }}
.horse-detail.show {{ display: block; }}
.race-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-top: 4px; }}
.race-card {{
  padding: 8px 9px; border-radius: 8px; border: 1px solid var(--border);
  background: rgba(255,255,255,0.05); font-size: 10px; cursor: pointer;
  transition: border-color 0.12s;
}}
.race-card.ideal {{ background: rgba(34,197,94,0.12); border-color: rgba(34,197,94,0.4); }}
.race-card.highlighted {{ border-color: var(--accent-cv) !important; box-shadow: 0 0 0 2px rgba(245,158,11,0.3); }}
.rc-date {{ color: var(--text-muted); font-weight: 600; font-family: monospace; font-size: 9px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.rc-name {{ color: var(--text); font-weight: 700; font-size: 10px; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.rc-mid-row {{ display: flex; justify-content: space-between; align-items: center; margin-top: 3px; }}
.rc-agari {{ font-size: 11px; color: var(--text-muted); }}
.rc-result {{ font-size: 13px; font-weight: 900; }}
.rc-winner {{ font-size: 11px; color: var(--text-muted); text-align: right; }}
.rc-sub {{ font-size: 11px; color: var(--text-muted); margin-top: 2px; }}
.race-mark-row {{ display: flex; gap: 3px; margin-top: 4px; justify-content: flex-end; }}
.race-mark-btn {{
  width: 26px; height: 22px; border: 1.5px solid var(--border);
  border-radius: 5px; background: var(--bg-card); cursor: pointer;
  font-size: 10px; font-weight: 800; color: var(--text-muted);
  transition: all 0.12s; -webkit-tap-highlight-color: transparent; padding: 0;
}}
.race-mark-btn:active {{ transform: none; }}
.race-mark-btn.marked-○ {{ background: #dc2626; border-color: #dc2626; color: #fff; }}
.race-mark-btn.marked-▲ {{ background: #f59e0b; border-color: #f59e0b; color: #fff; }}
.race-mark-btn.marked-× {{ background: #64748b; border-color: #64748b; color: #fff; }}

/* ── Tooltip ── */
.tooltip {{
  display: none; position: fixed;
  background: rgba(5,20,70,0.97); color: var(--text);
  border: 1px solid var(--border2);
  padding: 10px 13px; border-radius: 10px;
  font-size: 12px; line-height: 1.7; pointer-events: none;
  z-index: 300; max-width: 240px;
  box-shadow: 0 8px 32px rgba(0,10,60,0.7);
  backdrop-filter: blur(8px);
}}
.tooltip.show {{ display: block; }}
.tooltip-title {{ font-weight: 900; color: var(--accent-cv); margin-bottom: 2px; }}
.tooltip-result {{ font-weight: 900; font-size: 14px; }}
.tooltip-diff {{ font-size: 10px; color: var(--text-muted); margin-top: 2px; }}
</style>
</head>
<body>

<div class="header">
  <div class="header-row">
    <h1>{venue}{race_num}R　{race_name}　{surf_lbl}{distance}m　CV {target_cushion}　含水率 {target_moisture}%{f"　{start_time}" if start_time else ""}</h1>
  </div>
</div>

<div class="main">
  <div class="chart-wrap">
    <canvas id="chart"></canvas>
    <div class="tooltip" id="tooltip"></div>
  </div>
  <div class="panel" id="panel"></div>
</div>

<div class="legend">
  <div class="legend-item"><span class="ldot" style="background:#ef4444"></span>{color_same}</div>
  <div class="legend-item"><span class="ldot" style="background:#3b82f6"></span>{color_diff}</div>
  <div class="legend-item"><span class="ldot" style="background:#475569"></span>{color_other}</div>
  <div class="legend-item">○=3着以内　×=4着以下</div>
</div>

<script type="application/json" id="race-data">
{structured_json}
</script>
<div id="ai-readable" style="display:none" aria-hidden="true"><pre>{ai_text_summary}</pre></div>

<script>
const HORSES = {horses_json};
const RACE_ID = '{race_id_str}';
const TX = {target_cushion};
const TY = {target_moisture};
const WEATHER_RANGE = {weather_range_json};
const LINE_X = 9.5;
const LINE_Y = 12.0;
const TDIST = {distance};
const SURFACE = '{surface}';
const COLORS = {{ same_dist:'#ef4444', diff_dist:'#3b82f6', diff_surface:'#475569', target:'#f59e0b' }};
const RANK_COLORS = {{S:'#dc2626',A:'#f59e0b',B:'#3b82f6',C:'#22c55e',D:'#64748b'}};
const X_MIN = 7.0, X_MAX = 12.0;
const Y_MIN = 0, Y_MAX = 22;

let selectedHorses = new Set();
let highlightedPoints = new Set();
const canvas = document.getElementById('chart');
const ctx = canvas.getContext('2d');
const tooltipEl = document.getElementById('tooltip');
const horseColors = HORSES.map(h => h.waku_color || '#888888');

const STORAGE_KEY = 'v2_ratings_{venue}_{race_num}R_{race_name}';
const ratings = (function() {{ try {{ const s = localStorage.getItem(STORAGE_KEY); return s ? JSON.parse(s) : {{}}; }} catch(e) {{ return {{}}; }} }})();
function saveRatings() {{ try {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(ratings)); }} catch(e) {{}} }}

const RR_KEY = 'v2_raceRatings_{venue}_{race_num}R_{race_name}';
const raceRatings = (function() {{ try {{ const s = localStorage.getItem(RR_KEY); return s ? JSON.parse(s) : {{}}; }} catch(e) {{ return {{}}; }} }})();
function saveRaceRatings() {{ try {{ localStorage.setItem(RR_KEY, JSON.stringify(raceRatings)); }} catch(e) {{}} }}

function getW() {{ return canvas.width / (window.devicePixelRatio || 1); }}
function getH() {{ return canvas.height / (window.devicePixelRatio || 1); }}
const PAD = {{ l: 46, r: 16, t: 18, b: 36 }};

function toX(v) {{ const w = getW() - PAD.l - PAD.r; return PAD.l + (v - X_MIN) / (X_MAX - X_MIN) * w; }}
function toY(v) {{ const h = getH() - PAD.t - PAD.b; return PAD.t + (1 - (v - Y_MIN) / (Y_MAX - Y_MIN)) * h; }}

function resize() {{
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}}

function draw() {{
  const W = getW(), H = getH();
  ctx.clearRect(0, 0, W, H);

  // background
  ctx.fillStyle = '#0f172a';
  ctx.fillRect(0, 0, W, H);


  // grid lines
  ctx.lineWidth = 1;
  for (let x = Math.ceil(X_MIN * 2) / 2; x <= X_MAX; x += 0.5) {{
    const px = toX(x);
    const isMajor = Number.isInteger(x);
    ctx.strokeStyle = isMajor ? '#1e3a5f' : '#1a2d3f';
    ctx.beginPath(); ctx.moveTo(px, PAD.t); ctx.lineTo(px, H - PAD.b); ctx.stroke();
    if (isMajor || x % 1 === 0) {{
      ctx.fillStyle = '#475569'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
      ctx.fillText(x.toFixed(1), px, H - PAD.b + 14);
    }}
  }}
  for (let y = 0; y <= Y_MAX; y += 2) {{
    const py = toY(y);
    const isMajor = y % 4 === 0;
    ctx.strokeStyle = isMajor ? '#1e3a5f' : '#1a2d3f';
    ctx.beginPath(); ctx.moveTo(PAD.l, py); ctx.lineTo(W - PAD.r, py); ctx.stroke();
    ctx.fillStyle = '#475569'; ctx.font = '10px monospace'; ctx.textAlign = 'right';
    ctx.fillText(y + '%', PAD.l - 4, py + 4);
  }}

  // axis labels
  ctx.fillStyle = '#64748b'; ctx.font = 'bold 10px sans-serif'; ctx.textAlign = 'center';
  ctx.fillText('クッション値', PAD.l + (W - PAD.l - PAD.r) / 2, H - 4);
  ctx.save(); ctx.translate(11, PAD.t + (H - PAD.t - PAD.b) / 2);
  ctx.rotate(-Math.PI / 2); ctx.fillText('含水率（ゴール前）%', 0, 0); ctx.restore();

  // reference lines
  ctx.setLineDash([6, 4]); ctx.lineWidth = 1.5;
  if (SURFACE === '芝') {{
    ctx.strokeStyle = 'rgba(99,102,241,0.5)';
    const lx = toX(LINE_X);
    ctx.beginPath(); ctx.moveTo(lx, PAD.t); ctx.lineTo(lx, H - PAD.b); ctx.stroke();
    ctx.strokeStyle = 'rgba(20,184,166,0.5)';
    const ly = toY(LINE_Y);
    ctx.beginPath(); ctx.moveTo(PAD.l, ly); ctx.lineTo(W - PAD.r, ly); ctx.stroke();
  }} else {{
    [5, 10, 15].forEach(pct => {{
      ctx.strokeStyle = 'rgba(20,184,166,0.4)';
      const py = toY(pct);
      ctx.beginPath(); ctx.moveTo(PAD.l, py); ctx.lineTo(W - PAD.r, py); ctx.stroke();
    }});
  }}
  ctx.setLineDash([]);

  // data points
  const hlDeferred = [];
  HORSES.forEach((h, hi) => {{
    const isSel = selectedHorses.has(h.name);
    const dimmed = selectedHorses.size > 0 && !isSel;
    const alpha = dimmed ? 0.07 : (isSel ? 1.0 : 0.75);
    h.races.forEach((r, ri) => {{
      const isHL = highlightedPoints.has(hi + '-' + ri);
      if (isHL) {{ hlDeferred.push({{h, hi, r, ri, isSel, dimmed, alpha}}); return; }}
      drawPoint(r, isSel ? 15 : 10, alpha, COLORS[r.cat], false);
      if (!dimmed) drawLabel(r, isSel ? 13 : 9, COLORS[r.cat]);
    }});
  }});

  // highlighted points on top
  hlDeferred.forEach(item => {{
    const {{r, isSel}} = item;
    const sz = 18;
    ctx.globalAlpha = 1;
    ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 4;
    ctx.beginPath(); ctx.arc(toX(r.cushion), toY(r.moisture), sz + 5, 0, Math.PI * 2); ctx.stroke();
    drawPoint(r, sz, 1.0, COLORS[r.cat], true);
    drawLabel(r, 13, '#f59e0b');
  }});

  // rating marks on points
  ctx.globalAlpha = 1;
  HORSES.forEach((h, hi) => {{
    if (!ratings[h.name]) return;
    const rank = ratings[h.name];
    const rc = RANK_COLORS[rank];
    h.races.forEach(r => {{
      const alpha = selectedHorses.size > 0 && !selectedHorses.has(h.name) ? 0.15 : 1;
      ctx.globalAlpha = alpha;
      ctx.fillStyle = rc; ctx.font = 'bold 8px Arial'; ctx.textAlign = 'left';
      ctx.fillText(rank, toX(r.cushion) + 10, toY(r.moisture) - 8);
    }});
  }});

  // target star
  ctx.globalAlpha = 1;
  const starX = toX(TX), starY = toY(TY);
  ctx.fillStyle = '#f59e0b'; ctx.font = 'bold 22px Arial'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.strokeStyle = '#0f172a'; ctx.lineWidth = 3;
  ctx.strokeText('◆', starX, starY); ctx.fillText('◆', starX, starY);
  ctx.textBaseline = 'alphabetic';
}}

function drawPoint(r, sz, alpha, color, highlighted) {{
  const px = toX(r.cushion), py = toY(r.moisture);
  ctx.globalAlpha = alpha;
  if (r.good) {{
    ctx.beginPath(); ctx.arc(px, py, sz, 0, Math.PI * 2);
    ctx.fillStyle = highlighted ? '#1c2a1a' : '#0f172a';
    ctx.fill();
    ctx.strokeStyle = color; ctx.lineWidth = highlighted ? 3 : 2;
    ctx.stroke();
  }} else {{
    ctx.strokeStyle = color; ctx.lineWidth = highlighted ? 3 : 2;
    ctx.beginPath(); ctx.moveTo(px - sz, py - sz); ctx.lineTo(px + sz, py + sz); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(px + sz, py - sz); ctx.lineTo(px - sz, py + sz); ctx.stroke();
  }}
}}

function drawLabel(r, fs, color) {{
  const px = toX(r.cushion), py = toY(r.moisture);
  ctx.fillStyle = color; ctx.font = `bold ${{fs}}px Arial`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.strokeStyle = '#0f172a'; ctx.lineWidth = 2.5;
  ctx.strokeText(r.result !== null ? r.result : '?', px, py + 1);
  ctx.fillText(r.result !== null ? r.result : '?', px, py + 1);
  ctx.textBaseline = 'alphabetic'; ctx.globalAlpha = 1;
}}

function buildPanel() {{
  const panel = document.getElementById('panel');
  const RANKS = ['S', 'A', 'B', 'C', 'D'];
  let html = '';
  HORSES.forEach((h, i) => {{
    const cnt = h.races.length;
    const noData = cnt === 0;
    const numColor = h.waku_color || '#888888';
    const textColor = ['#FFFFFF','#F5C518','#F3A3BD'].includes(numColor) ? '#222' : '#fff';
    html += `<button class="horse-btn${{noData ? ' no-data' : ''}}" id="btn-${{i}}">
      <span class="h-num" style="background:${{numColor}};color:${{textColor}}">${{h.horse_num}}</span>
      <span>${{h.name}}</span>
      <span class="h-odds" id="odds-${{h.horse_num}}"></span>
      <span class="h-count">${{cnt > 0 ? cnt + '走' : 'なし'}}</span>
    </button>`;
    html += `<div class="rating-row" id="rate-${{i}}">`;
    RANKS.forEach(r => {{ html += `<button class="rating-btn" data-horse="${{i}}" data-rank="${{r}}">${{r}}</button>`; }});
    html += `</div>`;
    html += `<div class="horse-detail" id="detail-${{i}}">`;
    if (cnt > 0) {{
      html += `<div class="race-grid">`;
      h.races.forEach((r, ri) => {{
        const inIdeal = Math.abs(r.cushion - TX) <= 0.2 && Math.abs(r.moisture - TY) <= 1.5;
        const rrKey = h.name + '_' + ri;
        const distLabel = r.distance === TDIST ? '同' : (r.distance > TDIST ? '短' : '延');
        const resultColor = COLORS[r.cat] || '#888888';
        html += `<div class="race-card${{inIdeal ? ' ideal' : ''}}" data-horse="${{i}}" data-ri="${{ri}}" style="border-left:3px solid ${{numColor}}">
          <div class="rc-date">${{r.date}}　${{r.venue}}　CV${{r.cushion}}/${{r.moisture}}%</div>
          <div class="rc-name">${{r.race_name}}　${{r.surface}}${{r.distance}}m（${{distLabel}}）</div>
          <div class="rc-mid-row">
            <span class="rc-agari">${{(r.agari || r.passage) ? (r.agari ? '上がり ' + r.agari : '') + (r.agari && r.passage ? '　' : '') + (r.passage ? '通過 ' + r.passage : '') : ''}}</span>
            <span class="rc-result" style="color:${{resultColor}}">${{r.result !== null ? r.result + '着' : '取消'}}</span>
          </div>
          ${{r.winner ? `<div class="rc-winner">${{r.winner}}${{r.time_diff ? ' (' + r.time_diff + ')' : ''}}</div>` : ''}}
          <div class="race-mark-row" data-rrkey="${{rrKey}}">
            <button class="race-mark-btn" data-mark="○">○</button>
            <button class="race-mark-btn" data-mark="▲">▲</button>
            <button class="race-mark-btn" data-mark="×">×</button>
          </div>
        </div>`;
      }});
      html += `</div>`;
    }}
    html += `</div>`;
  }});
  panel.innerHTML = html;

  HORSES.forEach((h, i) => {{
    document.getElementById('btn-' + i).addEventListener('click', () => {{
      const detail = document.getElementById('detail-' + i);
      if (selectedHorses.has(h.name)) {{
        selectedHorses.delete(h.name);
        detail.classList.remove('show');
        document.getElementById('btn-' + i).classList.remove('selected');
      }} else {{
        selectedHorses.add(h.name);
        detail.classList.add('show');
        document.getElementById('btn-' + i).classList.add('selected');
      }}
      requestAnimationFrame(draw);
    }});
  }});

  document.querySelectorAll('.rating-btn').forEach(btn => {{
    btn.addEventListener('click', e => {{
      e.stopPropagation();
      const hi = parseInt(btn.dataset.horse);
      const rank = btn.dataset.rank;
      const name = HORSES[hi].name;
      if (ratings[name] === rank) {{ delete ratings[name]; }} else {{ ratings[name] = rank; }}
      updateRatings();
    }});
  }});

  document.querySelectorAll('.race-card').forEach(el => {{
    el.addEventListener('click', e => {{
      if (e.target.classList.contains('race-mark-btn')) return;
      e.stopPropagation();
      el.classList.toggle('highlighted');
      const key = el.dataset.horse + '-' + el.dataset.ri;
      if (highlightedPoints.has(key)) highlightedPoints.delete(key);
      else highlightedPoints.add(key);
      requestAnimationFrame(draw);
    }});
  }});

  document.querySelectorAll('.race-mark-btn').forEach(btn => {{
    btn.addEventListener('click', e => {{
      e.stopPropagation();
      const row = btn.closest('.race-mark-row');
      const rrKey = row.dataset.rrkey;
      const mark = btn.dataset.mark;
      if (raceRatings[rrKey] === mark) {{ delete raceRatings[rrKey]; }} else {{ raceRatings[rrKey] = mark; }}
      updateRaceMarks();
      saveRaceRatings();
    }});
  }});
}}

function updateRatings() {{
  document.querySelectorAll('.rating-btn').forEach(btn => {{
    const hi = parseInt(btn.dataset.horse);
    const rank = btn.dataset.rank;
    const name = HORSES[hi].name;
    btn.className = 'rating-btn' + (ratings[name] === rank ? ' rated-' + rank : '');
  }});
  saveRatings();
  draw();
}}

function updateRaceMarks() {{
  document.querySelectorAll('.race-mark-btn').forEach(btn => {{
    const row = btn.closest('.race-mark-row');
    const rrKey = row.dataset.rrkey;
    const mark = btn.dataset.mark;
    btn.className = 'race-mark-btn' + (raceRatings[rrKey] === mark ? ' marked-' + mark : '');
  }});
}}

const isMobile = 'ontouchstart' in window;

function getPointAt(cx, cy) {{
  let closest = null, minDist = isMobile ? 36 : 22;
  HORSES.forEach(h => {{
    if (selectedHorses.size > 0 && !selectedHorses.has(h.name)) return;
    h.races.forEach(r => {{
      const px = toX(r.cushion), py = toY(r.moisture);
      const d = Math.sqrt((cx - px) ** 2 + (cy - py) ** 2);
      if (d < minDist) {{ minDist = d; closest = {{...r, horse: h.name}}; }}
    }});
  }});
  return closest;
}}

function showTooltip(pt, sx, sy) {{
  if (!pt) {{ tooltipEl.classList.remove('show'); return; }}
  const cvDiff = (pt.cushion - TX).toFixed(2);
  const mDiff = (pt.moisture - TY).toFixed(1);
  const cvSign = cvDiff >= 0 ? '+' : '';
  const mSign = mDiff >= 0 ? '+' : '';
  const resultColor = pt.result === 1 ? '#f59e0b' : pt.result !== null && pt.result <= 3 ? '#22c55e' : '#ef4444';
  tooltipEl.innerHTML = `<div class="tooltip-title">${{pt.horse}}</div>
    ${{pt.date}} ${{pt.venue}} ${{pt.surface}}${{pt.distance}}m<br>
    ${{pt.race_name}}<br>
    <span class="tooltip-result" style="color:${{resultColor}}">${{pt.result !== null ? pt.result + '着' : '取消'}}</span>
    　CV: <b>${{pt.cushion}}</b>　含水率: <b>${{pt.moisture}}%</b>
    <div class="tooltip-diff">当日比: CV ${{cvSign}}${{cvDiff}} / 含水率 ${{mSign}}${{mDiff}}%</div>`;
  const left = Math.min(sx + 14, window.innerWidth - 260);
  const top = Math.max(sy - 50, 8);
  tooltipEl.style.left = left + 'px';
  tooltipEl.style.top = top + 'px';
  tooltipEl.classList.add('show');
}}

canvas.addEventListener('mousemove', e => {{
  const rect = canvas.getBoundingClientRect();
  showTooltip(getPointAt(e.clientX - rect.left, e.clientY - rect.top), e.clientX, e.clientY);
}});
canvas.addEventListener('mouseleave', () => tooltipEl.classList.remove('show'));

let touchTimer = null;
canvas.addEventListener('touchstart', e => {{
  const t = e.touches[0];
  const rect = canvas.getBoundingClientRect();
  showTooltip(getPointAt(t.clientX - rect.left, t.clientY - rect.top), t.clientX, t.clientY);
}}, {{ passive: true }});
canvas.addEventListener('touchmove', e => {{
  const t = e.touches[0];
  const rect = canvas.getBoundingClientRect();
  showTooltip(getPointAt(t.clientX - rect.left, t.clientY - rect.top), t.clientX, t.clientY);
}}, {{ passive: true }});
canvas.addEventListener('touchend', () => {{
  if (touchTimer) clearTimeout(touchTimer);
  touchTimer = setTimeout(() => tooltipEl.classList.remove('show'), 2200);
}});
canvas.addEventListener('click', e => {{
  const rect = canvas.getBoundingClientRect();
  showTooltip(getPointAt(e.clientX - rect.left, e.clientY - rect.top), e.clientX, e.clientY);
}});

window.addEventListener('resize', resize);
buildPanel();
updateRatings();
updateRaceMarks();
resize();

</script>
</body></html>"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    total_pts = sum(len(h['races']) for h in js_horses)
    horses_with_data = sum(1 for h in js_horses if h['races'])
    return total_pts, horses_with_data, len(js_horses)


# ===== インデックスページ生成 (ダークテーマ v2) =====
def generate_index(out_dir, results_summary, jra_live, date_label, date_str=''):
    """レース一覧インデックスページを生成（3カラムカードレイアウト）"""
    venues = {}
    for row in results_summary:
        venue, rnum, rname, total, pts, surf, dist = row[:7]
        grade_sfx = row[7] if len(row) > 7 else ''
        start_time = row[8] if len(row) > 8 else ''
        if venue not in venues:
            venues[venue] = []
        venues[venue].append((rnum, rname, total, pts, surf, dist, grade_sfx, start_time))

    venue_info = {}
    for venue, data in jra_live.items():
        c = data.get('cushion', '?')
        tm = data.get('turf_moisture', data.get('turf_goal', '?'))
        dm = data.get('dirt_moisture', data.get('dirt_goal', '?'))
        venue_info[venue] = {'cv': c, 'turf': tm, 'dirt': dm}

    # 日付ヘッダー用
    weekday_ja = ['月', '火', '水', '木', '金', '土', '日']
    try:
        dt = datetime.strptime(date_str, '%Y%m%d')
        date_header = f"{dt.month}/{dt.day}（{weekday_ja[dt.weekday()]}）"
    except Exception:
        date_header = date_label

    # 会場座標（天気取得用）
    VENUE_COORDS = {
        '中山': (35.78, 139.93), '東京': (35.68, 139.50), '阪神': (34.82, 135.37),
        '京都': (34.90, 135.72), '中京': (35.11, 136.93), '小倉': (33.87, 130.87),
        '新潟': (37.92, 139.05), '福島': (37.75, 140.43), '函館': (41.77, 140.72),
        '札幌': (43.07, 141.38),
    }

    html = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{date_header} クッション値×含水率</title>
<style>
:root {{
  --bg:#132044; --bg-card:#1a2c5a; --bg-row:#152348;
  --border:#253d72; --text:#e8eef8; --text-sub:#8da8d8; --text-muted:#4a6090;
  --accent:#f59e0b; --green:#22c55e; --amber:#f59e0b;
}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Noto Sans JP',sans-serif;
  background:var(--bg);color:var(--text);padding:12px;
  -webkit-font-smoothing:antialiased;}}
/* ヘッダー */
.top-header{{display:flex;align-items:center;gap:10px;margin-bottom:14px;padding:4px 0;}}
.top-header h1{{font-size:22px;font-weight:900;letter-spacing:-0.5px;}}
.week-badge{{background:#3b5bdb;color:#fff;font-size:11px;font-weight:800;
  padding:3px 10px;border-radius:20px;}}
/* グリッド */
.venue-grid{{
  display:grid;
  grid-template-columns:repeat(3,minmax(320px,1fr));
  gap:12px;
  overflow-x:auto;
  -webkit-overflow-scrolling:touch;
  padding-bottom:4px;
}}
/* カード */
.venue-card{{border:1px solid var(--border);border-radius:12px;overflow:hidden;}}
.venue-head{{padding:10px 13px 8px;background:var(--bg-card);}}
.venue-title{{display:flex;align-items:baseline;gap:8px;margin-bottom:5px;flex-wrap:wrap;}}
.venue-title h2{{font-size:17px;font-weight:900;}}
.cv-inline{{font-size:11px;color:var(--text-sub);font-weight:600;}}
.cv-inline b{{color:var(--accent);}}
.cv-inline .t{{color:var(--green);}}
.cv-inline .d{{color:var(--amber);}}
/* 天気 */
.wx-row{{display:flex;gap:12px;}}
.wx-slot{{display:flex;flex-direction:column;align-items:center;gap:1px;}}
.wx-slot .wx-h{{font-size:9px;color:var(--text-muted);}}
.wx-slot .wx-icon{{font-size:17px;line-height:1;}}
/* レース行 */
.race-list a{{
  display:flex;align-items:center;justify-content:space-between;
  padding:11px 13px;border-top:1px solid var(--border);
  background:var(--bg-row);color:var(--text);text-decoration:none;
  transition:background 0.12s;-webkit-tap-highlight-color:transparent;
  overflow:hidden;
}}
.race-list a:hover{{background:var(--bg-card);}}
.race-left{{display:flex;align-items:center;gap:8px;min-width:0;flex:1;overflow:hidden;}}
.race-num-wrap{{display:flex;flex-direction:column;align-items:flex-start;min-width:36px;flex-shrink:0;}}
.race-num{{font-size:13px;font-weight:800;color:var(--text-sub);}}
.race-time{{font-size:9px;color:var(--text-muted);font-weight:600;white-space:nowrap;}}
.race-name{{font-size:13px;font-weight:700;white-space:nowrap;}}
.sbadge{{font-size:10px;font-weight:800;padding:2px 7px;border-radius:5px;flex-shrink:0;white-space:nowrap;}}
.sbadge.turf{{background:rgba(34,197,94,0.18);color:#4ade80;border:1px solid rgba(34,197,94,0.35);}}
.sbadge.dirt{{background:rgba(245,158,11,0.18);color:#fbbf24;border:1px solid rgba(245,158,11,0.35);}}
.race-dist{{font-size:12px;color:var(--text-sub);font-weight:600;flex-shrink:0;white-space:nowrap;}}
.grade-badge{{font-size:9px;font-weight:900;padding:1px 5px;border-radius:4px;
  background:rgba(220,38,38,0.25);color:#fca5a5;border:1px solid rgba(220,38,38,0.4);}}
.arrow{{color:var(--text-muted);font-size:14px;}}
/* ランプ */
.lamp{{width:8px;height:8px;border-radius:50%;flex-shrink:0;
  background:#374151;box-shadow:none;transition:background 0.3s,box-shadow 0.3s;}}
.lamp.on{{background:#22c55e;box-shadow:0 0 6px 2px rgba(34,197,94,0.55);}}
.lamp.soon{{background:#ef4444;box-shadow:0 0 6px 2px rgba(239,68,68,0.6);}}
</style>
</head>
<body>
<div class="top-header">
  <h1>{date_header}</h1>
  <span class="week-badge">今週</span>
</div>
<div class="venue-grid" id="grid">
'''

    active_venues = [v for v in ['東京', '中山', '阪神', '京都', '中京', '小倉', '新潟', '福島', '函館', '札幌'] if v in venues]

    for venue in active_venues:
        info = venue_info.get(venue, {})
        cv = info.get('cv', '?')
        turf = info.get('turf', '?')
        dirt = info.get('dirt', '?')
        lat, lon = VENUE_COORDS.get(venue, (35.68, 139.50))

        html += f'''<div class="venue-card">
  <div class="venue-head">
    <div class="venue-title">
      <h2>{venue}</h2>
      <span class="cv-inline">CV=<b>{cv}</b>&nbsp; 芝<span class="t">{turf}%</span>&nbsp; ダ<span class="d">{dirt}%</span></span>
    </div>
    <div class="wx-row" id="wx-{venue}">
      <div class="wx-slot"><span class="wx-h">9時</span><span class="wx-icon" data-h="9">—</span></div>
      <div class="wx-slot"><span class="wx-h">12時</span><span class="wx-icon" data-h="12">—</span></div>
      <div class="wx-slot"><span class="wx-h">15時</span><span class="wx-icon" data-h="15">—</span></div>
    </div>
  </div>
  <div class="race-list">
'''
        for row in sorted(venues[venue]):
            rnum, rname, total, pts, surf, dist = row[:6]
            grade_sfx = row[6] if len(row) > 6 else ''
            stime = row[7] if len(row) > 7 else ''
            raw_name = rname
            clean_name = re.sub(r'(芝|ダ|障)\d+m', '', raw_name)
            clean_name = re.sub(r'\d+頭', '', clean_name)
            clean_name = re.sub(r'^0?\d+R', '', clean_name).strip()
            safe_name = (clean_name or raw_name).replace('/', '_').replace(' ', '')
            fname = f'scatter_{date_str}_{venue}{rnum:02d}R_{safe_name}_{surf}{dist}m{grade_sfx}.html'
            surf_class = 'turf' if surf == '芝' else 'dirt'
            grade_label = f'<span class="grade-badge">{grade_sfx[1:]}</span>' if grade_sfx else ''
            time_label = f'<span class="race-time">{stime}</span>' if stime else ''
            data_time = f'data-start="{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}T{stime}"' if stime else ''
            html += f'''    <a href="{fname}">
      <div class="race-left">
        <span class="lamp" {data_time}></span>
        <div class="race-num-wrap"><span class="race-num">{rnum}R</span>{time_label}</div>
        <span class="race-name">{rname}</span>
        {grade_label}
        <span class="sbadge {surf_class}">{surf}</span>
        <span class="race-dist">{dist}m</span>
      </div>
      <span class="arrow">›</span>
    </a>
'''
        html += f'  </div>\n</div>\n'

    # 天気アイコン取得JS（Open-Meteo API）
    venue_coords_js = ', '.join(
        f'"{v}": [{VENUE_COORDS[v][0]}, {VENUE_COORDS[v][1]}]'
        for v in active_venues if v in VENUE_COORDS
    )
    html += f'''</div>
<script>
const VENUE_COORDS = {{{venue_coords_js}}};
const TODAY = '{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}';
function wmoIcon(c) {{
  if (c === 0) return '☀️';
  if (c <= 2) return '🌤️';
  if (c <= 3) return '☁️';
  if (c <= 49) return '🌫️';
  if (c <= 67) return '🌧️';
  if (c <= 77) return '❄️';
  if (c <= 82) return '🌦️';
  return '⛈️';
}}
Object.entries(VENUE_COORDS).forEach(([venue, [lat, lon]]) => {{
  const el = document.getElementById('wx-' + venue);
  if (!el) return;
  const url = 'https://api.open-meteo.com/v1/forecast?latitude=' + lat +
    '&longitude=' + lon + '&hourly=weather_code&timezone=Asia%2FTokyo&forecast_days=1';
  fetch(url).then(r => r.json()).then(data => {{
    const times = data.hourly.time;
    const codes = data.hourly.weather_code;
    el.querySelectorAll('.wx-icon').forEach(slot => {{
      const h = parseInt(slot.dataset.h);
      const ts = TODAY + 'T' + String(h).padStart(2,'0') + ':00';
      const idx = times.indexOf(ts);
      if (idx >= 0) slot.textContent = wmoIcon(codes[idx]);
    }});
  }}).catch(() => {{}});
}});
// ランプ更新（30秒ごと）
function updateLamps() {{
  const now = new Date();
  const lamps = Array.from(document.querySelectorAll('.lamp[data-start]'));
  // 未来のレースのうち現在時刻に最も近いものを特定
  let nearest = null, nearestDiff = Infinity;
  lamps.forEach(el => {{
    const diff = new Date(el.dataset.start) - now;
    if (diff >= 0 && diff < nearestDiff) {{ nearestDiff = diff; nearest = el; }}
  }});
  lamps.forEach(el => {{
    const diff = new Date(el.dataset.start) - now;
    el.classList.remove('on', 'soon');
    if (el === nearest) el.classList.add('soon');  // 直近レース: 赤
    else if (diff > 0) el.classList.add('on');     // その他未来: 緑
    // 発走済: グレー（デフォルト）
  }});
}}
updateLamps();
setInterval(updateLamps, 30000);
</script>
</body></html>'''

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
        print('  {"github_token": "ghp_xxx", "repo": "user/repo-name"} の形式で作成してください')
        return

    with open(DEPLOY_CONFIG_PATH, encoding='utf-8') as f:
        config = json.load(f)

    cushion_db = {}
    if os.path.exists(CUSHION_DB_PATH):
        with open(CUSHION_DB_PATH, encoding='utf-8') as f:
            cushion_db = json.load(f)

    token = config['github_token']
    repo = config['repo']
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
    }
    api_base = f'https://api.github.com/repos/{repo}/contents'

    def _fetch_all_files(url, hdrs):
        result = {}
        page = 1
        while True:
            r = requests.get(url, headers=hdrs, params={'per_page': 100, 'page': page})
            if r.status_code != 200:
                break
            items = r.json()
            if not items:
                break
            for item in items:
                result[item['name']] = item['sha']
            if len(items) < 100:
                break
            page += 1
        return result

    print(f"  リポジトリ: {repo}")
    existing = _fetch_all_files(api_base, headers)

    html_files = [f for f in os.listdir(out_dir) if f.endswith('.html')]
    for fname in sorted(html_files):
        fpath = os.path.join(out_dir, fname)
        with open(fpath, 'rb') as f:
            content = base64.b64encode(f.read()).decode()

        encoded_name = quote(fname)
        url = f'{api_base}/{encoded_name}'
        payload = {'message': f'Update {fname} ({date_str})', 'content': content}
        if fname in existing:
            payload['sha'] = existing[fname]

        r = requests.put(url, headers=headers, json=payload)
        if r.status_code in (200, 201):
            print(f"  OK {fname}")
        else:
            try:
                msg = r.json().get('message', '')
            except Exception:
                msg = r.text[:100]
            print(f"  NG {fname}: {r.status_code} {msg}")
        time.sleep(1)

    if cleanup:
        print(f"\n  旧ファイル削除中...")
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
                        print(f"  Del {fname}")
                    time.sleep(1)

    # index.html をリモートに合わせて再生成してアップロード
    all_files = _fetch_all_files(api_base, headers)

    all_scatter = sorted([f for f in all_files if f.startswith('scatter_') and f.endswith('.html')], reverse=True)
    date_groups = {}
    for fname in all_scatter:
        m = re.match(r'scatter_(\d{8})_(.+)\.html', fname)
        if m:
            d = m.group(1)
            d_fmt = f"{d[:4]}/{d[4:6]}/{d[6:8]}"
            date_groups.setdefault(d_fmt, []).append(fname)
        else:
            date_groups.setdefault('その他', []).append(fname)

    index_html = _build_remote_index(date_groups, cushion_db)

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
        print(f"  OK index.html")

    pages_url = f'https://{repo.split("/")[0]}.github.io/{repo.split("/")[1]}/'
    print(f"\n  デプロイ完了!")
    print(f"  URL: {pages_url}")


def _build_remote_index(date_groups, cushion_db):
    """リモートの全ファイルから日付別インデックスHTMLを生成 (横スクロール会場カラムレイアウト)"""
    from datetime import datetime
    WEEKDAYS = ['月', '火', '水', '木', '金', '土', '日']
    venue_order = ['東京', '京都', '小倉', '中山', '阪神', '中京', '新潟', '福島', '函館', '札幌']
    FILE_RE = re.compile(r'^scatter_(\d{8})_([\u4e00-\u9fff]+)(\d{2})R_(.+)_([ダ芝障])(\d+)m(?:_(\d{4}))?(?:_(G[123]))?\.html$')

    html = r'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>クッション値×含水率 散布図</title>
<style>
:root {
  --bg-start: #0a1e4a; --bg-mid: #0f2d6b; --bg-end: #0a1a3d;
  --bg-card: rgba(255,255,255,0.07); --bg-card2: rgba(255,255,255,0.04);
  --border: rgba(100,160,255,0.2); --border-bright: rgba(100,160,255,0.4);
  --text: #e8f0ff; --text-muted: #7ea8d8; --text-sub: #a8c4e8;
  --accent: #f59e0b; --accent2: #38bdf8;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Noto Sans JP', sans-serif;
  background: linear-gradient(160deg, var(--bg-start) 0%, var(--bg-mid) 50%, var(--bg-end) 100%);
  background-attachment: fixed;
  min-height: 100vh;
  color: var(--text); padding: 14px;
  -webkit-font-smoothing: antialiased;
}
/* ── Header ── */
.page-header {
  margin-bottom: 14px; display: flex;
  justify-content: space-between; align-items: flex-start;
  background: linear-gradient(135deg, rgba(255,255,255,0.1), rgba(255,255,255,0.04));
  border: 1px solid var(--border-bright);
  border-radius: 14px; padding: 14px 16px;
  backdrop-filter: blur(10px);
}
.page-header h1 {
  font-size: 18px; font-weight: 900;
  background: linear-gradient(90deg, #ffffff, #7eb8f7);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.page-header .sub { font-size: 11px; color: var(--text-muted); margin-top: 3px; }
.admin-btn {
  display: inline-flex; align-items: center; gap: 6px;
  background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.22);
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  color: var(--text-sub); padding: 7px 12px; border-radius: 10px;
  font-size: 12px; font-weight: 800; white-space: nowrap;
  cursor: pointer; transition: all 0.15s; flex-shrink: 0;
  box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}
.admin-btn:hover { border-color: var(--accent); color: var(--accent); background: rgba(245,158,11,0.15); }
.admin-dot { width: 7px; height: 7px; border-radius: 50%; background: #4a6a8a; flex-shrink: 0; }
.admin-btn.online .admin-dot { background: #22c55e; animation: pulse 1.5s infinite; }
.admin-btn.online { border-color: rgba(34,197,94,0.4); color: #22c55e; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
/* ── Filter bar ── */
.filter-bar {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 14px; flex-wrap: wrap;
  background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: 14px; padding: 10px 12px;
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  box-shadow: 0 2px 12px rgba(0,0,0,0.2);
}
.stab {
  padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 800;
  cursor: pointer; border: 1px solid rgba(255,255,255,0.18);
  background: rgba(255,255,255,0.10); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  color: var(--text-sub); transition: all 0.15s;
  box-shadow: 0 1px 4px rgba(0,0,0,0.15);
}
.stab:hover { border-color: rgba(255,255,255,0.35); color: var(--text); background: rgba(255,255,255,0.16); }
.stab.active { background: var(--accent); border-color: var(--accent); color: #000; backdrop-filter: none; }
.graded-label {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 12px; border-radius: 20px; font-size: 13px; font-weight: 800;
  border: 1px solid rgba(255,255,255,0.18); background: rgba(255,255,255,0.10);
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  color: var(--text-sub); cursor: pointer; user-select: none; transition: all 0.15s;
  box-shadow: 0 1px 4px rgba(0,0,0,0.15);
}
.graded-label input { display: none; }
.graded-label.checked { background: rgba(124,58,237,0.3); border-color: rgba(167,139,250,0.5); color: #c4b5fd; backdrop-filter: blur(8px); }
.venue-select {
  margin-left: auto; padding: 6px 10px; border-radius: 8px;
  background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.22);
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  color: var(--text); font-size: 12px; font-weight: 700; cursor: pointer;
  box-shadow: 0 1px 4px rgba(0,0,0,0.15);
}
.venue-select option { background: #0f2d6b; }
/* ── Date section ── */
.date-section {
  margin-bottom: 12px; border-radius: 14px; overflow: hidden;
  border: 1px solid var(--border-bright);
  box-shadow: 0 4px 24px rgba(0,30,80,0.4);
}
.date-header {
  padding: 12px 16px;
  background: linear-gradient(135deg, rgba(30,80,160,0.5), rgba(15,45,107,0.5));
  cursor: pointer; display: flex; align-items: center; gap: 8px;
  font-size: 15px; font-weight: 900; user-select: none;
  -webkit-tap-highlight-color: transparent;
  backdrop-filter: blur(8px);
}
.badge-week {
  font-size: 10px; font-weight: 800; padding: 2px 7px; border-radius: 10px;
  background: rgba(34,197,94,0.15); color: #4ade80; border: 1px solid rgba(34,197,94,0.3);
}
.race-count { font-size: 12px; color: var(--text-muted); font-weight: 600; }
.date-header .spacer { flex: 1; }
.date-header .toggle { font-size: 11px; color: var(--text-muted); transition: transform 0.2s; }
.date-header.open .toggle { transform: rotate(180deg); }
.race-list {
  display: none; padding: 10px;
  background: linear-gradient(180deg, rgba(10,30,74,0.6), rgba(8,20,55,0.8));
  backdrop-filter: blur(4px);
}
.race-list.open { display: block; }
/* ── Venue grid (horizontal scroll) ── */
.venue-grid {
  display: flex; gap: 10px;
  overflow-x: auto; padding-bottom: 6px;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: thin; scrollbar-color: rgba(100,160,255,0.3) transparent;
}
.venue-grid::-webkit-scrollbar { height: 4px; }
.venue-grid::-webkit-scrollbar-track { background: transparent; }
.venue-grid::-webkit-scrollbar-thumb { background: rgba(100,160,255,0.3); border-radius: 2px; }
.venue-col {
  min-width: 220px; flex-shrink: 0;
  background: rgba(255,255,255,0.06);
  border-radius: 10px;
  border: 1px solid var(--border); overflow: hidden;
  backdrop-filter: blur(6px);
}
.venue-col-header {
  padding: 8px 12px;
  background: linear-gradient(135deg, rgba(56,100,200,0.35), rgba(30,60,140,0.35));
  border-bottom: 1px solid var(--border-bright);
}
.venue-head-row {
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
}
.venue-col-header .venue-name {
  font-size: 14px; font-weight: 900;
  color: #c8deff;
}
.venue-col-header .venue-cv {
  font-size: 10px; color: var(--text-muted); font-weight: 600;
}
.venue-weather {
  display: flex; gap: 6px; margin-top: 5px; flex-wrap: wrap;
}
.wx-slot {
  font-size: 10px; color: #a8c4e8; font-weight: 600;
  display: flex; flex-direction: column; align-items: center; gap: 1px;
  min-width: 28px;
}
.wx-slot .wx-time { font-size: 8px; color: #7ea8d8; }
.wx-slot .wx-icon { font-size: 16px; line-height: 1;
  font-family: 'Segoe UI Emoji','Apple Color Emoji','Noto Color Emoji','Twemoji Mozilla',sans-serif; }
/* ── Race row ── */
.race-row {
  display: flex; align-items: center; gap: 6px;
  padding: 10px 12px; border-bottom: 1px solid var(--border);
  color: var(--text); text-decoration: none; font-size: 13px;
  -webkit-tap-highlight-color: transparent; transition: background 0.1s;
}
.race-row:last-child { border-bottom: none; }
.race-row:active, .race-row:hover { background: rgba(100,160,255,0.12); }
.race-num-block { display: flex; flex-direction: column; align-items: center; min-width: 36px; flex-shrink: 0; gap: 2px; }
.race-num { font-size: 11px; font-weight: 800; color: var(--text-muted); white-space: nowrap; }
.race-time-row { display: flex; align-items: center; gap: 3px; }
.race-time { font-size: 9px; color: var(--accent2); font-weight: 700; white-space: nowrap; font-family: monospace; }
.time-lamp { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; background: #2d3f55; }
.time-lamp.lamp-green { background: #22c55e; box-shadow: 0 0 4px #22c55e; }
.time-lamp.lamp-red { background: #ef4444; box-shadow: 0 0 5px #ef4444; }
.race-name { flex: 1; font-weight: 700; font-size: 12px; line-height: 1.3; }
.race-dist { font-size: 10px; color: var(--text-muted); font-weight: 600; white-space: nowrap; }
.arrow { color: var(--text-muted); font-size: 14px; flex-shrink: 0; }
/* ── Surface badges ── */
.sbadge {
  font-size: 10px; font-weight: 900; padding: 2px 6px;
  border-radius: 6px; flex-shrink: 0;
}
.sbadge.turf { background: rgba(34,197,94,0.2); color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }
.sbadge.dirt { background: rgba(245,158,11,0.2); color: #fbbf24; border: 1px solid rgba(245,158,11,0.4); }
.sbadge.hurdle { background: rgba(148,163,184,0.15); color: #94a3b8; border: 1px solid rgba(148,163,184,0.3); }
/* ── Admin modal ── */
.admin-overlay {
  display: none; position: fixed; inset: 0;
  background: rgba(0,10,40,0.75); z-index: 200;
  align-items: center; justify-content: center;
  backdrop-filter: blur(4px);
}
.admin-overlay.show { display: flex; }
.admin-modal {
  background: linear-gradient(135deg, rgba(20,50,120,0.95), rgba(10,30,80,0.95));
  border: 1px solid var(--border-bright); border-radius: 16px;
  padding: 24px; max-width: 360px; width: 90%;
  box-shadow: 0 20px 60px rgba(0,10,50,0.7);
  backdrop-filter: blur(12px);
}
.admin-modal h3 { font-size: 16px; font-weight: 900; margin-bottom: 8px; color: #c8deff; }
.admin-modal p { font-size: 13px; color: var(--text-sub); line-height: 1.6; margin-bottom: 16px; }
.admin-modal code {
  display: block; background: rgba(0,0,0,0.3); border: 1px solid var(--border-bright);
  border-radius: 8px; padding: 10px 12px; font-size: 12px;
  color: var(--accent); font-family: monospace; margin-bottom: 16px;
}
.modal-btns { display: flex; gap: 8px; }
.modal-btn {
  flex: 1; padding: 10px; border-radius: 8px; font-size: 13px;
  font-weight: 800; cursor: pointer; border: none; transition: all 0.15s;
}
.modal-btn-primary { background: var(--accent); color: #000; }
.modal-btn-primary:hover { background: #fbbf24; }
.modal-btn-secondary { background: rgba(255,255,255,0.1); color: var(--text); border: 1px solid var(--border); }
.modal-btn-secondary:hover { background: rgba(255,255,255,0.15); }
</style>
</head>
<body>

<div class="page-header">
  <div>
    <h1>クッション値×含水率 散布図</h1>
    <div class="sub">会場別レース一覧</div>
  </div>
  <button class="admin-btn" id="admin-btn" onclick="openAdmin()">
    <span class="admin-dot"></span>管理ページ
  </button>
</div>

<div class="filter-bar">
  <button class="stab active" data-filter="all">全て</button>
  <button class="stab" data-filter="turf">芝</button>
  <button class="stab" data-filter="dirt">ダート</button>
  <label class="graded-label" id="graded-label">
    <input type="checkbox" id="graded-toggle"> 重賞
  </label>
  <select class="venue-select" id="venue-select">
    <option value="">全会場</option>
  </select>
</div>

<div id="admin-modal" class="admin-overlay">
  <div class="admin-modal">
    <h3>🏇 管理ダッシュボード</h3>
    <p id="modal-msg">ローカルサーバーが起動しているか確認しています...</p>
    <code>start_admin.bat をダブルクリックして起動</code>
    <div class="modal-btns">
      <button class="modal-btn modal-btn-primary" id="modal-open-btn" onclick="goAdmin()">開く</button>
      <button class="modal-btn modal-btn-secondary" onclick="closeModal()">閉じる</button>
    </div>
  </div>
</div>
'''

    from datetime import date, timedelta
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    this_week_dates = {
        (monday + timedelta(5)).strftime('%Y/%m/%d'),
        (monday + timedelta(6)).strftime('%Y/%m/%d'),
    }

    today_fmt = date.today().strftime('%Y/%m/%d')
    date_keys = sorted(date_groups.keys(), reverse=True)
    if today_fmt in date_keys:
        date_keys.remove(today_fmt)
        date_keys.insert(0, today_fmt)
    all_venues_seen = []

    for idx, d_fmt in enumerate(date_keys):
        files_in_date = sorted(date_groups[d_fmt])
        try:
            dt = datetime.strptime(d_fmt, '%Y/%m/%d')
            day_label = f"{dt.month}/{dt.day}（{WEEKDAYS[dt.weekday()]}）"
        except ValueError:
            day_label = d_fmt
        count = len(files_in_date)
        open_cls = ' open' if d_fmt == today_fmt else ''

        venue_groups = {}
        for fname in files_in_date:
            m = FILE_RE.match(fname)
            if m:
                venue = m.group(2)
            else:
                vm = re.match(r'^scatter_\d{8}_([\u4e00-\u9fff]+)', fname)
                venue = vm.group(1) if vm else 'その他'
            venue_groups.setdefault(venue, []).append(fname)

        sorted_venues = sorted(venue_groups.keys(), key=lambda v: venue_order.index(v) if v in venue_order else 99)
        for v in sorted_venues:
            if v not in all_venues_seen:
                all_venues_seen.append(v)

        html += f'<div class="date-section" data-date="{d_fmt}">\n'
        html += f'<div class="date-header{open_cls}" onclick="toggleDate(this)">'
        html += f'<span>{day_label}</span>'
        if d_fmt in this_week_dates:
            html += '<span class="badge-week">今週</span>'
        html += '<span class="spacer"></span><span class="toggle">▼</span>'
        html += '</div>\n'
        html += f'<div class="race-list{open_cls}"><div class="venue-grid">\n'

        for venue_name in sorted_venues:
            vfiles = sorted(venue_groups[venue_name])
            d_key = f"{d_fmt}_{venue_name}"
            cv_val = 'CV未取得'
            if d_key in cushion_db:
                e = cushion_db[d_key]
                cv_val = f'CV={e.get("cushion","?")}  芝{e.get("turf_goal","?")}%  ダ{e.get("dirt_goal","?")}%'

            # 同一レース番号は時刻付きを優先して重複排除
            dedup = {}
            for fname in vfiles:
                m = FILE_RE.match(fname)
                key = m.group(3) if m else fname
                if key not in dedup or (m and m.group(7)):
                    dedup[key] = fname
            vfiles = sorted(dedup.values())

            html += f'<div class="venue-col" data-venue="{venue_name}">\n'
            weather_html = ''
            if d_fmt == today_fmt:
                weather_html = (f'<div class="venue-weather" data-wxvenue="{venue_name}">'
                                f'<span class="wx-slot"><span class="wx-time">9時</span><span class="wx-icon" data-wxh="9">…</span></span>'
                                f'<span class="wx-slot"><span class="wx-time">12時</span><span class="wx-icon" data-wxh="12">…</span></span>'
                                f'<span class="wx-slot"><span class="wx-time">15時</span><span class="wx-icon" data-wxh="15">…</span></span>'
                                f'</div>')
            html += f'<div class="venue-col-header"><div class="venue-head-row"><span class="venue-name">{venue_name}</span><span class="venue-cv">{cv_val}</span></div>{weather_html}</div>\n'

            for fname in vfiles:
                m = FILE_RE.match(fname)
                if m:
                    rnum = str(int(m.group(3)))
                    rname = m.group(4)
                    surf_char = m.group(5)
                    dist = m.group(6)
                    raw_time = m.group(7) or ''
                    start_time = f'{raw_time[:2]}:{raw_time[2:]}' if len(raw_time) == 4 else ''
                    if surf_char == '芝':
                        surf_cls, surf_lbl, surf_data = 'turf', '芝', 'turf'
                    elif surf_char == 'ダ':
                        surf_cls, surf_lbl, surf_data = 'dirt', 'ダ', 'dirt'
                    else:
                        surf_cls, surf_lbl, surf_data = 'hurdle', '障', 'hurdle'
                    is_graded = '1' if m.group(8) else '0'
                    if start_time:
                        time_inner = (f'<span class="race-time-row">'
                                      f'<span class="time-lamp"></span>'
                                      f'<span class="race-time">{start_time}</span>'
                                      f'</span>')
                    else:
                        time_inner = ''
                    st_attr = f' data-starttime="{raw_time}"' if raw_time else ''
                    html += (f'<a class="race-row" href="{fname}" data-surface="{surf_data}" data-graded="{is_graded}"{st_attr}>'
                             f'<span class="race-num-block"><span class="race-num">{rnum}R</span>{time_inner}</span>'
                             f'<span class="race-name">{rname}</span>'
                             f'<span class="sbadge {surf_cls}">{surf_lbl}</span>'
                             f'<span class="race-dist">{dist}m</span>'
                             f'<span class="arrow">›</span></a>\n')
                else:
                    display = fname.replace('scatter_', '').replace('.html', '')
                    html += (f'<a class="race-row" href="{fname}" data-surface="all" data-graded="0">'
                             f'<span class="race-name">{display}</span>'
                             f'<span class="arrow">›</span></a>\n')

            html += '</div>\n'  # venue-col

        html += '</div></div></div>\n'  # venue-grid / race-list / date-section

    venue_opts = ''.join(f'<option value="{v}">{v}</option>'
                         for v in sorted(all_venues_seen, key=lambda v: venue_order.index(v) if v in venue_order else 99))

    html += r'''<script>
function toggleDate(el) {
  el.classList.toggle('open');
  el.nextElementSibling.classList.toggle('open');
}

// Populate venue select
(function() {
  var sel = document.getElementById('venue-select');
  document.querySelectorAll('.venue-col').forEach(function(col) {
    var v = col.dataset.venue;
    if (v && !sel.querySelector('option[value="' + v + '"]')) {
      var opt = document.createElement('option');
      opt.value = v; opt.textContent = v;
      sel.appendChild(opt);
    }
  });
})();

var surfaceFilter = 'all';
var gradedOnly = false;
var venueFilter = '';

function applyFilters() {
  document.querySelectorAll('.race-row').forEach(function(row) {
    var surf = row.dataset.surface;
    var graded = row.dataset.graded === '1';
    var show = true;
    if (surfaceFilter !== 'all' && surf !== surfaceFilter) show = false;
    if (gradedOnly && !graded) show = false;
    row.style.display = show ? '' : 'none';
  });
  document.querySelectorAll('.venue-col').forEach(function(col) {
    var vMatch = !venueFilter || col.dataset.venue === venueFilter;
    var hasVisible = vMatch && Array.from(col.querySelectorAll('.race-row')).some(function(r) {
      return r.style.display !== 'none';
    });
    col.style.display = hasVisible ? '' : 'none';
  });
}

document.querySelectorAll('.stab').forEach(function(btn) {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.stab').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    surfaceFilter = btn.dataset.filter;
    applyFilters();
  });
});

document.getElementById('graded-toggle').addEventListener('change', function(e) {
  gradedOnly = e.target.checked;
  document.getElementById('graded-label').classList.toggle('checked', gradedOnly);
  applyFilters();
});

document.getElementById('venue-select').addEventListener('change', function(e) {
  venueFilter = e.target.value;
  applyFilters();
});

// Admin
var ADMIN_URL = 'http://localhost:5001/';
function checkAdmin() {
  fetch(ADMIN_URL + 'api/status', { signal: AbortSignal.timeout(1500) })
    .then(function(r) { if (r.ok) document.getElementById('admin-btn').classList.add('online'); })
    .catch(function() {});
}
function openAdmin() {
  document.getElementById('admin-modal').classList.add('show');
  fetch(ADMIN_URL + 'api/status', { signal: AbortSignal.timeout(1500) })
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(data) {
      var msg = document.getElementById('modal-msg');
      var btn = document.getElementById('modal-open-btn');
      if (data) {
        msg.textContent = '✅ サーバー起動中です。管理ページを開けます。';
        msg.style.color = '#22c55e';
      } else {
        msg.textContent = '⚠️ サーバーが起動していません。先に start_admin.bat を実行してください。';
        msg.style.color = '#f59e0b';
      }
      btn.disabled = false;
    })
    .catch(function() {
      document.getElementById('modal-msg').textContent = '⚠️ サーバーが起動していません。先に start_admin.bat を実行してください。';
      document.getElementById('modal-msg').style.color = '#f59e0b';
    });
}
function goAdmin() { window.open(ADMIN_URL, '_blank'); closeModal(); }
function closeModal() { document.getElementById('admin-modal').classList.remove('show'); }
document.getElementById('admin-modal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});
checkAdmin();

// ── Race lamps ──
function updateLamps() {
  var now = new Date();
  var todayStr = now.getFullYear() + '/' +
    String(now.getMonth()+1).padStart(2,'0') + '/' +
    String(now.getDate()).padStart(2,'0');
  var nowMin = now.getHours() * 60 + now.getMinutes();

  // 今日のレース行を収集
  var todayRows = [];
  document.querySelectorAll('.date-section').forEach(function(sec) {
    if (sec.dataset.date !== todayStr) return;
    sec.querySelectorAll('.race-row[data-starttime]').forEach(function(row) {
      var t = row.dataset.starttime;
      todayRows.push({
        row: row,
        startMin: parseInt(t.slice(0,2),10)*60 + parseInt(t.slice(2,4),10)
      });
    });
  });

  // 全ランプをいったん消灯
  document.querySelectorAll('.time-lamp').forEach(function(l) {
    l.className = 'time-lamp';
  });

  if (todayRows.length === 0) return;

  // 直近レースを特定（未開始の中で nowMin に最も近いもの）
  var nearest = null, nearestDiff = Infinity;
  todayRows.forEach(function(r) {
    if (r.startMin <= nowMin) return; // 開始済みはスキップ
    var diff = r.startMin - nowMin;
    if (diff < nearestDiff) { nearestDiff = diff; nearest = r; }
  });

  todayRows.forEach(function(r) {
    var lamp = r.row.querySelector('.time-lamp');
    if (!lamp) return;
    if (r.startMin <= nowMin) {
      // 開始済み → 消灯
    } else if (r === nearest) {
      lamp.className = 'time-lamp lamp-red';  // 直近 → 赤
    } else {
      lamp.className = 'time-lamp lamp-green'; // 未来 → 緑
    }
  });
}
updateLamps();
setInterval(updateLamps, 60000);

// ── 天気取得 ──
(function() {
  var VENUE_COORDS = {
    '東京':[35.6955,139.4903],'中山':[35.7756,139.9297],'阪神':[34.8167,135.3833],
    '京都':[34.9000,135.7667],'中京':[35.1667,136.9333],'小倉':[33.8833,130.8333],
    '新潟':[37.8833,138.9500],'福島':[37.7667,140.4667],'函館':[41.7500,140.6833],'札幌':[43.0000,141.3500]
  };
  function wmoIcon(code) {
    if (code === 0) return '☀';
    if (code <= 1) return '🌤';
    if (code <= 2) return '⛅';
    if (code <= 3) return '☁';
    if (code <= 48) return '🌫';
    if (code <= 57) return '🌦';
    if (code <= 67) return '🌧';
    if (code <= 77) return '🌨';
    if (code <= 82) return '🌦';
    return '⛈';
  }
  var today = new Date();
  var todayStr = today.getFullYear() + '-' +
    String(today.getMonth()+1).padStart(2,'0') + '-' +
    String(today.getDate()).padStart(2,'0');
  document.querySelectorAll('[data-wxvenue]').forEach(function(el) {
    var venue = el.dataset.wxvenue;
    var coords = VENUE_COORDS[venue];
    if (!coords) return;
    var url = 'https://api.open-meteo.com/v1/forecast?latitude=' + coords[0] +
      '&longitude=' + coords[1] + '&hourly=weather_code&timezone=Asia%2FTokyo&forecast_days=1';
    fetch(url).then(function(r) { return r.json(); }).then(function(data) {
      var times = data.hourly.time;
      var codes = data.hourly.weather_code;
      [9, 12, 15].forEach(function(h) {
        var ts = todayStr + 'T' + String(h).padStart(2,'0') + ':00';
        var idx = times.indexOf(ts);
        if (idx < 0) return;
        var icon = wmoIcon(codes[idx]);
        el.querySelectorAll('[data-wxh]').forEach(function(slot) {
          if (parseInt(slot.dataset.wxh) === h) slot.textContent = icon;
        });
      });
    }).catch(function() {});
  });
})();
</script>
</body></html>'''
    return html


# ===== メインパイプライン =====
def main():
    parser = argparse.ArgumentParser(description='競馬クッション値×含水率 散布図 一括生成 v2')
    parser.add_argument('date', help='開催日 (YYYYMMDD)')
    parser.add_argument('--venue', help='競馬場で絞り込み (東京/京都/小倉 等)')
    parser.add_argument('--race', type=int, help='レース番号で絞り込み (例: 11)')
    parser.add_argument('--no-scrape', action='store_true', help='（廃止・互換用）キャッシュが使われる')
    parser.add_argument('--output', default=None, help='出力先ディレクトリ')
    parser.add_argument('--deploy', action='store_true', help='GitHub Pagesへ自動デプロイ')
    parser.add_argument('--manual', action='store_true', help='クッション値・含水率を会場別に手動入力')
    parser.add_argument('--force-update', action='store_true', help='既存キーの上書きを許可')
    parser.add_argument('--cleanup', action='store_true', help='旧フォーマットファイルをGitHubから削除')
    args = parser.parse_args()

    date_str = args.date
    date_label = f"{date_str[4:6]}/{date_str[6:8]}"
    out_dir = args.output or os.path.join(OUTPUT_DIR, date_str)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    print("=" * 60)
    print(f"[Step 0] JRA重賞グレード取得")
    print("=" * 60)
    jra_graded = fetch_jra_graded_races()
    print(f"  取得: {len(jra_graded)}レース {list(jra_graded.items())[:5]}")

    print("=" * 60)
    print(f"[Step 1] レース一覧取得 ({date_str})")
    print("=" * 60)

    _jra_race_data_map = {}
    if _JRA_AVAILABLE:
        print("  JRA公式（JRADB）から取得中...")
        races, _jra_race_data_map = fetch_jra_races(date_str)
    else:
        races = []

    if not races:
        print("  JRA取得失敗 → キャッシュから復元を試みます...")
        cache_files = [f for f in os.listdir(CACHE_DIR)
                       if f.startswith(f'race_jra_{date_str}_') and f.endswith('.json')]
        for cf in sorted(cache_files):
            with open(os.path.join(CACHE_DIR, cf), encoding='utf-8') as fp:
                cd = json.load(fp)
            ri = cd.get('race_info', {})
            rid = ri.get('race_id', cf.replace('race_', '').replace('.json', ''))
            _jra_race_data_map[rid] = cd
            races.append({
                'race_id':    rid,
                'venue':      ri.get('venue', '?'),
                'race_num':   int(rid.split('_')[-1]) if rid.split('_')[-1].isdigit() else 0,
                'race_name':  ri.get('race_name', ''),
                'surface':    ri.get('surface', '?'),
                'distance':   ri.get('distance', 0),
                'start_time': ri.get('start_time', ''),
                'text':       '',
            })
        if races:
            print(f"  キャッシュから{len(races)}レース復元")
        else:
            print("  キャッシュもありません。終了します。")
            sys.exit(1)

    if args.venue:
        races = [r for r in races if r['venue'] == args.venue]
    if args.race:
        races = [r for r in races if r['race_num'] == args.race]
    races = [r for r in races if r['surface'] != '障']

    print(f"  対象: {len(races)}レース")
    for r in races:
        print(f"    {r['venue']}{r['race_num']}R {r['race_name']} {r['surface']}{r['distance']}m")
    print()

    print("=" * 60)
    print(f"[Step 2] クッション値・含水率 取得")
    print("=" * 60)
    if args.manual:
        venues_in_races = sorted(set(r['venue'] for r in races))
        jra_live = {}
        print(f"  *** 手動入力モード ({len(venues_in_races)}会場) ***\n")
        for v in venues_in_races:
            print(f"  [{v}]")
            cv = input(f"    クッション値 (例: 9.5): ")
            mt = input(f"    芝 含水率% (例: 12.0): ")
            md = input(f"    ダート 含水率% (例: 5.0): ")
            jra_live[v] = {'cushion': float(cv), 'turf_moisture': float(mt), 'dirt_moisture': float(md)}
            print(f"    → CV={cv} 芝={mt}% ダ={md}%\n")
    else:
        jra_live = fetch_jra_live()
        for venue, data in jra_live.items():
            c = data.get('cushion', '?')
            tm = data.get('turf_moisture', '?')
            dm = data.get('dirt_moisture', '?')
            print(f"  {venue}: CV={c}  芝={tm}%  ダ={dm}%")
    print()

    print("=" * 60)
    print(f"[Step 3] クッション値DB読み込み")
    print("=" * 60)
    with open(CUSHION_DB_PATH, encoding='utf-8') as f:
        cushion_db = json.load(f)
    print(f"  DB件数: {len(cushion_db)}")

    date_fmt = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:8]}"
    today = datetime.now().strftime('%Y%m%d')
    is_today = (date_str == today)
    added = 0

    if not is_today and not args.manual:
        print(f"  ※ {date_str}は今日({today})ではないためDB蓄積をスキップ")
    else:
        for venue, data in jra_live.items():
            key = f"{date_fmt}_{venue}"
            cushion_val = data.get('cushion')
            if cushion_val is None or cushion_val == 0.0:
                print(f"  ※ {venue}のクッション値が不正({cushion_val})のためスキップ")
                continue
            if key not in cushion_db or args.force_update:
                cushion_db[key] = {
                    'date': date_fmt, 'venue': venue, 'cushion': cushion_val,
                    'turf_goal': data.get('turf_moisture'), 'dirt_goal': data.get('dirt_moisture'),
                }
                added += 1
        if added > 0:
            with open(CUSHION_DB_PATH, 'w', encoding='utf-8') as f:
                json.dump(cushion_db, f, ensure_ascii=False, indent=2)
            print(f"  → {added}件追加（合計: {len(cushion_db)}件）")
        else:
            print(f"  → 既存データのため追加なし")
    print()

    print("=" * 60)
    print(f"[Step 4] 各レース処理")
    print("=" * 60)
    results_summary = []

    for race in races:
        rid = race['race_id']
        venue = race['venue']
        race_num = race['race_num']
        surface = race['surface']

        print(f"\n--- {venue} {race_num}R {race['race_name']} {surface}{race['distance']}m ---")

        cache_file = os.path.join(CACHE_DIR, f'race_{rid}.json')
        if rid in _jra_race_data_map:
            race_data = _jra_race_data_map[rid]
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(race_data, f, ensure_ascii=False, indent=2)
        elif os.path.exists(cache_file):
            print(f"  キャッシュ使用: {cache_file}")
            with open(cache_file, encoding='utf-8') as f:
                race_data = json.load(f)
        else:
            print(f"  SKIP: JRAデータ未取得かつキャッシュなし")
            continue

        if not race_data.get('race_info', {}).get('race_name'):
            race_data.setdefault('race_info', {})['race_name'] = race['race_name']
        if not race_data['race_info'].get('surface'):
            race_data['race_info']['surface'] = surface
        if not race_data['race_info'].get('distance'):
            race_data['race_info']['distance'] = race['distance']
        race_data['race_info']['start_time'] = race.get('start_time', '')

        race_data = link_cushion_data(race_data, cushion_db)

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
        time4 = race.get('start_time', '').replace(':', '')
        time_suffix = f'_{time4}' if time4 else ''
        grade = match_grade(race['race_name'], jra_graded)
        grade_suffix = f'_{grade}' if grade else ''
        output_file = os.path.join(out_dir, f'scatter_{date_str}_{venue}{race_num:02d}R_{safe_name}_{surface}{race["distance"]}m{time_suffix}{grade_suffix}.html')

        new_basename = os.path.basename(output_file)
        prefix = f'scatter_{date_str}_{venue}{race_num:02d}R_'
        for old_f in os.listdir(out_dir):
            if old_f.startswith(prefix) and old_f.endswith('.html') and old_f != new_basename:
                os.remove(os.path.join(out_dir, old_f))
                print(f"  旧ファイル削除: {old_f}")

        w_range = compute_weather_range(venue, surface, date_str)
        pts, with_data, total = generate_scatter_html(
            race_data, target_cushion, target_moisture,
            output_file, date_label=date_label, race_num=race_num,
            race_date=date_str, weather_range=w_range,
        )
        print(f"  → 生成: {total}頭 ({with_data}頭データあり) {pts}pts")
        results_summary.append((venue, race_num, race['race_name'], total, pts, surface, race['distance'], grade_suffix, race.get('start_time', '')))

    print()
    print("=" * 60)
    print("完了サマリー")
    print("=" * 60)
    for row in results_summary:
        venue, rnum, rname, total, pts, surf, dist = row[:7]
        print(f"  {venue}{rnum:2d}R {rname:20s} {surf}{dist}m {total}頭 {pts}pts")
    print(f"\n  出力先: {out_dir}")
    print(f"  合計: {len(results_summary)}レース")

    generate_index(out_dir, results_summary, jra_live, date_label, date_str)

    if args.deploy:
        deploy_to_github(out_dir, date_str, cleanup=args.cleanup)


if __name__ == '__main__':
    main()
