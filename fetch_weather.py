#!/usr/bin/env python3
"""
気象庁から競馬場最寄り観測所の日別気象データを取得
"""

import requests
import time
from bs4 import BeautifulSoup

# 会場 → 気象庁観測所 (prec_no, block_no, 観測所名)
STATION_MAP = {
    'tokyo':     (44, 47662, '東京'),
    'nakayama':  (45, 47682, '千葉'),
    'hanshin':   (63, 47770, '神戸'),
    'kyoto':     (61, 47759, '京都'),
    'chukyo':    (51, 47636, '名古屋'),
    'fukushima': (36, 47595, '福島'),
    'niigata':   (54, 47604, '新潟'),
    'kokura':    (82, 47813, '北九州'),
    'sapporo':   (14, 47412, '札幌'),
    'hakodate':  (14, 47430, '函館'),
}

_cache = {}  # {(venue_en, year, month): {day: {...}}}


def fetch_jma_monthly(venue_en, year, month):
    """気象庁から月別日別データを取得。{day(int): dict} を返す"""
    key = (venue_en, year, month)
    if key in _cache:
        return _cache[key]

    st = STATION_MAP.get(venue_en)
    if not st:
        return {}

    prec, block, _ = st
    url = (
        f'https://www.data.jma.go.jp/obd/stats/etrn/view/daily_s1.php'
        f'?prec_no={prec}&block_no={block}&year={year}&month={month}&day=&view='
    )
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.encoding = 'utf-8'
    except Exception:
        return {}

    soup = BeautifulSoup(r.text, 'html.parser')
    table = soup.find('table', id='tablefix1')
    if not table:
        return {}

    result = {}
    for row in table.find_all('tr'):
        cells = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
        if not cells:
            continue
        try:
            day = int(cells[0])
        except ValueError:
            continue
        if len(cells) < 20:
            continue

        def safe_float(s):
            try:
                return float(s)
            except (ValueError, TypeError):
                return None

        result[day] = {
            'rainfall_24h':   safe_float(cells[3]),
            'temperature_avg': safe_float(cells[6]),
            'humidity_avg':    safe_float(cells[9]),
            'wind_speed_avg':  safe_float(cells[11]),
            'weather':         cells[19] if len(cells) > 19 else '',
        }

    _cache[key] = result
    return result


def fill_weather(obs_rows, on_progress=None):
    """observations リストに気象データを埋め込む。更新件数を返す"""
    updated = 0
    # venue+year+month の組み合わせを収集
    groups = {}
    for row in obs_rows:
        date = row.get('date', '')
        if len(date) < 10:
            continue
        # 既にデータが入っているならスキップ
        if row.get('temperature_avg') or row.get('humidity_avg'):
            continue
        venue = row.get('venue', '')
        if venue not in STATION_MAP:
            continue
        y, m, d = date[:4], date[5:7], date[8:10]
        k = (venue, int(y), int(m))
        groups.setdefault(k, []).append((int(d), row))

    total = len(groups)
    done = 0
    for (venue, year, month), day_rows in groups.items():
        daily = fetch_jma_monthly(venue, year, month)
        for day, row in day_rows:
            data = daily.get(day)
            if not data:
                continue
            for field in ('rainfall_24h', 'temperature_avg', 'humidity_avg',
                          'wind_speed_avg', 'weather'):
                val = data.get(field)
                if val is not None and val != '':
                    row[field] = str(val) if not isinstance(val, str) else val
            updated += 1
        done += 1
        if on_progress:
            on_progress(done, total)
        time.sleep(0.5)

    return updated
