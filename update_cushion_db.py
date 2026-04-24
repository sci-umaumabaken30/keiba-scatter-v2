#!/usr/bin/env python3
"""
JRA公式からクッション値・含水率を取得してcushion_db_full.jsonを更新するスクリプト

データソース:
  1. JRAライブページ（現在開催中の全会場・全計測日のデータ）
  2. JRA公式PDF（開催終了後に公開されるアーカイブ）

使い方:
  python update_cushion_db.py                    # ライブ+PDFから全データを取得・補完
  python update_cushion_db.py --year 2025        # 2025年のPDFデータを取得
  python update_cushion_db.py --year 2025 2026   # 複数年のPDF
"""

import requests
import json
import re
import os
import sys
import time
import argparse
from datetime import datetime

try:
    import fitz  # pymupdf
except ImportError:
    print("[ERROR] pymupdfが必要です。以下のコマンドでインストールしてください:")
    print("  pip install pymupdf")
    sys.exit(1)

CUSHION_DB_PATH = os.path.join(os.path.dirname(__file__), 'cushion_db_full.json')

VENUE_MAP = {
    'sapporo': '札幌', 'hakodate': '函館', 'fukushima': '福島', 'niigata': '新潟',
    'tokyo': '東京', 'nakayama': '中山', 'chukyo': '中京', 'kyoto': '京都',
    'hanshin': '阪神', 'kokura': '小倉'
}


def fetch_pdf(year, venue_en, kai):
    """JRA公式PDFをダウンロード"""
    url = f"https://www.jra.go.jp/keiba/baba/archive/{year}pdf/{venue_en}{kai:02d}.pdf"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.content
    except requests.RequestException:
        pass
    return None


def parse_cushion_pdf(pdf_bytes):
    """PDFからクッション値・含水率をパース"""
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    text = ''
    for page in doc:
        text += page.get_text()
    lines = text.strip().split('\n')

    # ヘッダーから年と会場を抽出
    header = lines[0] if lines else ''
    year_match = re.search(r'(\d{4})年\s*(\d+)回(.+?)競馬', header)
    if not year_match:
        return []
    year = year_match.group(1)
    venue = year_match.group(3)

    records = []
    i = 0
    while i < len(lines):
        date_match = re.match(r'\s*(\d{1,2})月\s*(\d{1,2})日', lines[i])
        if date_match:
            month = int(date_match.group(1))
            day = int(date_match.group(2))
            date_str = f'{year}/{month:02d}/{day:02d}'
            try:
                cushion = float(lines[i + 4].strip())
                turf_goal = float(lines[i + 6].strip())
                dirt_goal = float(lines[i + 8].strip())

                if cushion > 0:
                    records.append({
                        'date': date_str,
                        'venue': venue,
                        'cushion': cushion,
                        'turf_goal': turf_goal,
                        'dirt_goal': dirt_goal,
                    })
                i += 10
                # 「第X日」行があればスキップ
                if i < len(lines) and re.match(r'第\s*\d+日', lines[i]):
                    i += 1
            except (IndexError, ValueError):
                i += 1
        else:
            i += 1
    return records


def parse_cushion_pdf_legacy(pdf_bytes, venue_en):
    """旧形式PDF（2024年以前）パーサー
    構造: 第X日・第X日（YYYY年M月D日～D日）+ 金土日 3列形式
    """
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    text = ''
    for page in doc:
        text += page.get_text()
    lines = text.strip().split('\n')
    venue_ja = VENUE_MAP.get(venue_en, venue_en)
    records = []
    DAY_RE   = re.compile(r'[金土日月火水木]曜日')
    FLOAT_RE = re.compile(r'^\d+\.\d+$')

    i = 0
    while i < len(lines):
        # ブロックヘッダー: YYYY年M月D日～D日
        hm = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日[^\d]+(\d{1,2})日', lines[i])
        if not hm:
            i += 1
            continue
        year      = int(hm.group(1))
        month     = int(hm.group(2))
        start_day = int(hm.group(3))
        end_day   = int(hm.group(4))
        day_count = end_day - start_day + 1
        if day_count <= 0 or day_count > 4:
            i += 1
            continue

        from datetime import date as _date, timedelta
        base = _date(year, month, start_day)
        dates = [(base + timedelta(days=d)).strftime('%Y/%m/%d') for d in range(day_count)]

        j = i + 1
        # 曜日ヘッダーをスキップ
        while j < len(lines) and DAY_RE.match(lines[j]):
            j += 1

        def read_floats(start, n):
            vals, k = [], start
            while k < len(lines) and len(vals) < n:
                if FLOAT_RE.match(lines[k].strip()):
                    vals.append(float(lines[k].strip()))
                k += 1
            return vals, k

        # クッション値
        cushion_vals, j = read_floats(j, day_count)

        # 芝ゴール前含水率
        while j < len(lines) and '芝コース' not in lines[j]:
            j += 1
        j += 1  # skip 芝コース含水率
        while j < len(lines) and 'ゴール前' not in lines[j]:
            j += 1
        j += 1  # skip ゴール前
        turf_vals, j = read_floats(j, day_count)

        # ダートゴール前含水率
        while j < len(lines) and 'ダート' not in lines[j]:
            j += 1
        j += 1  # skip ダートコース含水率
        while j < len(lines) and 'ゴール前' not in lines[j]:
            j += 1
        j += 1  # skip ゴール前
        dirt_vals, j = read_floats(j, day_count)

        for d_idx, date_str in enumerate(dates):
            cushion = cushion_vals[d_idx] if d_idx < len(cushion_vals) else None
            turf_mo = turf_vals[d_idx]    if d_idx < len(turf_vals)    else None
            dirt_mo = dirt_vals[d_idx]    if d_idx < len(dirt_vals)    else None
            if cushion and cushion > 0:
                records.append({
                    'date': date_str, 'venue': venue_ja,
                    'cushion': cushion, 'turf_goal': turf_mo, 'dirt_goal': dirt_mo,
                })

        i = j

    return records


def fetch_jra_live_history():
    """JRAライブページから現在開催中の全会場・全計測日のクッション値+含水率を取得"""
    from bs4 import BeautifulSoup

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    records = []
    year = datetime.now().year

    # クッション値取得
    cushion_data = {}  # {(venue, date_str): cushion}
    try:
        r = requests.get('https://www.jra.go.jp/keiba/baba/_data_cushion.html',
                         headers=headers, timeout=15)
        r.encoding = 'shift_jis'
        soup = BeautifulSoup(r.text, 'html.parser')

        for div in soup.find_all('div', id=re.compile(r'^rc[A-Z]')):
            venue = div.get('title', '')
            if not venue:
                continue
            for unit in div.find_all('div', class_='unit'):
                time_div = unit.find('div', class_='time')
                cushion_div = unit.find('div', class_='cushion')
                if not time_div or not cushion_div:
                    continue
                time_text = time_div.get_text(strip=True)
                cushion_text = cushion_div.get_text(strip=True)
                if not cushion_text:
                    continue
                # 日付パース: "3月7日（土曜）7時00分" → "2026/03/07"
                m = re.match(r'(\d{1,2})月(\d{1,2})日', time_text)
                if m:
                    month = int(m.group(1))
                    day = int(m.group(2))
                    date_str = f"{year}/{month:02d}/{day:02d}"
                    try:
                        cushion_data[(venue, date_str)] = float(cushion_text)
                    except ValueError:
                        pass
    except Exception as e:
        print(f"  ※ クッション値ライブ取得エラー: {e}")

    # 含水率取得
    moist_data = {}  # {(venue, date_str): (turf_goal, dirt_goal)}
    try:
        r = requests.get('https://www.jra.go.jp/keiba/baba/_data_moist.html',
                         headers=headers, timeout=15)
        r.encoding = 'shift_jis'
        soup = BeautifulSoup(r.text, 'html.parser')

        for div in soup.find_all('div', id=re.compile(r'^rc[A-Z]')):
            venue = div.get('title', '')
            if not venue:
                continue
            for unit in div.find_all('div', class_='unit'):
                time_div = unit.find('div', class_='time')
                turf_div = unit.find('div', class_='turf')
                dirt_div = unit.find('div', class_='dirt')
                if not time_div:
                    continue
                time_text = time_div.get_text(strip=True)
                m = re.match(r'(\d{1,2})月(\d{1,2})日', time_text)
                if m:
                    month = int(m.group(1))
                    day = int(m.group(2))
                    date_str = f"{year}/{month:02d}/{day:02d}"
                    turf_mg = None
                    dirt_mg = None
                    if turf_div:
                        mg = turf_div.find('span', class_='mg')
                        if mg:
                            try:
                                turf_mg = float(mg.get_text(strip=True))
                            except ValueError:
                                pass
                    if dirt_div:
                        mg = dirt_div.find('span', class_='mg')
                        if mg:
                            try:
                                dirt_mg = float(mg.get_text(strip=True))
                            except ValueError:
                                pass
                    moist_data[(venue, date_str)] = (turf_mg, dirt_mg)
    except Exception as e:
        print(f"  ※ 含水率ライブ取得エラー: {e}")

    today_str = datetime.now().strftime('%Y/%m/%d')

    # クッション値と含水率を結合
    for (venue, date_str), cushion in cushion_data.items():
        if cushion == 0.0:
            continue
        # 未来日のデータは保存しない（JRAが先行公開する可能性があるが不正確な場合あり）
        if date_str > today_str:
            print(f"  ※ 未来日のためスキップ: {date_str} {venue}")
            continue
        turf_goal, dirt_goal = moist_data.get((venue, date_str), (None, None))
        records.append({
            'date': date_str,
            'venue': venue,
            'cushion': cushion,
            'turf_goal': turf_goal,
            'dirt_goal': dirt_goal,
        })

    return records


def update_db(years=None, db_path=None):
    """指定年のJRA PDFからクッション値を取得してDBを更新"""
    if years is None:
        years = [datetime.now().year]
    if db_path is None:
        db_path = CUSHION_DB_PATH

    # DB読み込み
    if os.path.exists(db_path):
        with open(db_path, encoding='utf-8') as f:
            cushion_db = json.load(f)
    else:
        cushion_db = {}

    print(f"既存DB: {len(cushion_db)}件")
    total_added = 0

    # ソース1: JRAライブページ（現在開催中の全会場・全計測日）
    print(f"\n=== JRAライブページから取得 ===")
    live_records = fetch_jra_live_history()
    live_added = 0
    for rec in live_records:
        key = f"{rec['date']}_{rec['venue']}"
        if key not in cushion_db:
            cushion_db[key] = rec
            live_added += 1
    if live_added > 0:
        print(f"  {live_added}件追加")
    else:
        print(f"  追加なし（既に最新）")
    total_added += live_added

    # ソース2: JRA公式PDF（開催終了後のアーカイブ）
    for year in years:
        print(f"\n=== {year}年 ===")
        for venue_en, venue_ja in VENUE_MAP.items():
            for kai in range(1, 7):
                pdf_bytes = fetch_pdf(year, venue_en, kai)
                if pdf_bytes is None:
                    continue
                time.sleep(0.5)

                records = parse_cushion_pdf(pdf_bytes)
                added = 0
                for rec in records:
                    key = f"{rec['date']}_{rec['venue']}"
                    if key not in cushion_db:
                        cushion_db[key] = rec
                        added += 1
                if added > 0:
                    print(f"  {venue_ja} {kai}回: {len(records)}日分取得, {added}件追加")
                    total_added += added

    if total_added > 0:
        with open(db_path, 'w', encoding='utf-8') as f:
            json.dump(cushion_db, f, ensure_ascii=False, indent=2)
        print(f"\n更新完了: {total_added}件追加 (合計: {len(cushion_db)}件)")
        print(f"保存先: {db_path}")
    else:
        print(f"\n追加なし (既に最新: {len(cushion_db)}件)")

    return total_added


def main():
    parser = argparse.ArgumentParser(description='JRA公式PDFからクッション値DBを更新')
    parser.add_argument('--year', type=int, nargs='+', default=[datetime.now().year],
                        help='取得する年 (デフォルト: 今年)')
    parser.add_argument('--db', default=CUSHION_DB_PATH,
                        help=f'DBファイルパス (デフォルト: {CUSHION_DB_PATH})')
    args = parser.parse_args()

    print("=" * 60)
    print("JRA クッション値DB 自動更新ツール")
    print("=" * 60)

    update_db(years=args.year, db_path=args.db)


if __name__ == '__main__':
    main()
