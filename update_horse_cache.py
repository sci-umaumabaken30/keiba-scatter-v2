"""
週次馬成績キャッシュ更新スクリプト
- race_results_cache.db の最近レースに出走した全馬を対象に
  horse_results_cache.db を最新成績で更新する
- レース後（翌日以降）に実行することで time_diff などが正確な値になる

使い方:
    python update_horse_cache.py            # 直近14日分を更新
    python update_horse_cache.py --days 7   # 直近7日分のみ
    python update_horse_cache.py --limit 50 # 1回あたり最大50頭
"""

import os, sys, json, sqlite3, time, re, argparse
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
HORSE_CACHE_DB = os.path.join(BASE_DIR, 'horse_results_cache.db')
RACE_CACHE_DIR = os.path.join(BASE_DIR, 'cache')

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def _db_connect():
    conn = sqlite3.connect(HORSE_CACHE_DB)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS horse_results (
            horse_id TEXT PRIMARY KEY,
            results_json TEXT,
            updated_at TEXT
        )
    ''')
    conn.commit()
    return conn


def _collect_horse_ids_from_cache(days: int) -> set:
    """race_jra_*.json から直近 days 日以内の horse_ids を収集する"""
    cutoff = datetime.now() - timedelta(days=days)
    horse_ids = set()

    if not os.path.isdir(RACE_CACHE_DIR):
        print(f"  [WARN] キャッシュディレクトリが見つかりません: {RACE_CACHE_DIR}")
        return horse_ids

    for fname in os.listdir(RACE_CACHE_DIR):
        if not fname.endswith('.json'):
            continue
        fpath = os.path.join(RACE_CACHE_DIR, fname)
        try:
            with open(fpath, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue

        # ファイル日付チェック（race_info.race_id から）
        race_id = data.get('race_info', {}).get('race_id', '')
        if len(race_id) >= 8:
            try:
                file_date = datetime.strptime(race_id[:8], '%Y%m%d')
                if file_date < cutoff:
                    continue
            except ValueError:
                pass

        # horse_ids フィールドがあれば直接使う
        hids = data.get('horse_ids', {})
        for hid in hids.values():
            if hid:
                horse_ids.add(hid)

    print(f"  キャッシュから {len(horse_ids)} 頭の horse_id を収集")
    return horse_ids


def fetch_horse_results(session, horse_id: str, max_races: int = 30) -> list:
    """netkeiba から馬の過去成績を取得"""
    url = f'https://db.netkeiba.com/horse/{horse_id}/'
    try:
        r = session.get(url, timeout=15)
        r.encoding = 'euc-jp'
    except Exception as e:
        print(f"    [ERROR] {horse_id}: {e}")
        return []

    soup = BeautifulSoup(r.text, 'html.parser')
    table = soup.find('table', class_='db_h_race_results')
    if not table:
        return []

    results = []
    for row in table.find_all('tr')[1:max_races + 1]:
        cells = row.find_all('td')
        if len(cells) < 20:
            continue
        try:
            date_raw = cells[0].get_text(strip=True)
            date_str = date_raw.replace('年', '/').replace('月', '/').replace('日', '')
            venue_raw = cells[1].get_text(strip=True)
            race_name = cells[4].get_text(strip=True)
            result_raw = cells[11].get_text(strip=True)
            result = int(result_raw) if result_raw.isdigit() else None
            time_diff_raw = cells[17].get_text(strip=True) if len(cells) > 17 else ''
            odds_raw = cells[20].get_text(strip=True) if len(cells) > 20 else ''
            surface_raw = cells[13].get_text(strip=True) if len(cells) > 13 else ''
            surface = '芝' if '芝' in surface_raw else 'ダ'
            dist_match = re.search(r'(\d{3,4})', surface_raw)
            distance = int(dist_match.group(1)) if dist_match else 0
            results.append({
                'date':      date_str,
                'venue':     venue_raw,
                'race_name': race_name,
                'result':    result,
                'time_diff': time_diff_raw,
                'surface':   surface,
                'distance':  distance,
                'odds':      odds_raw,
            })
        except Exception:
            continue
    return results


def update_cache(days: int = 14, limit: int = 200, sleep_sec: float = 1.5):
    conn = _db_connect()
    horse_ids = _collect_horse_ids_from_cache(days)

    if not horse_ids:
        print("  更新対象の horse_id が見つかりませんでした。")
        conn.close()
        return

    # 既存の updated_at と比較し、今日更新済みはスキップ
    today = datetime.now().strftime('%Y-%m-%d')
    existing = {
        hid: updated
        for hid, updated in conn.execute('SELECT horse_id, updated_at FROM horse_results').fetchall()
    }

    targets = []
    for hid in horse_ids:
        last = existing.get(hid, '')
        if last and last >= today:
            continue  # 今日すでに更新済み
        targets.append(hid)

    targets = targets[:limit]
    print(f"  更新対象: {len(targets)} 頭（スキップ: {len(horse_ids) - len(targets)} 頭）")

    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

    updated = skipped = errors = 0
    for i, hid in enumerate(targets, 1):
        results = fetch_horse_results(session, hid)
        if not results:
            errors += 1
            print(f"  [{i}/{len(targets)}] {hid}: 取得失敗")
            time.sleep(sleep_sec)
            continue

        conn.execute(
            'INSERT OR REPLACE INTO horse_results (horse_id, results_json, updated_at) VALUES (?,?,?)',
            (hid, json.dumps(results, ensure_ascii=False), today)
        )
        conn.commit()
        updated += 1
        print(f"  [{i}/{len(targets)}] {hid}: {len(results)}走 更新")
        time.sleep(sleep_sec)

    conn.close()
    print(f"\n完了: 更新={updated}頭  スキップ={skipped}頭  エラー={errors}頭")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='馬成績キャッシュ週次更新')
    parser.add_argument('--days',  type=int, default=14, help='直近何日分を対象にするか（デフォルト14）')
    parser.add_argument('--limit', type=int, default=200, help='1回あたりの最大更新頭数')
    parser.add_argument('--sleep', type=float, default=1.5, help='リクエスト間隔（秒）')
    args = parser.parse_args()

    print(f'馬成績キャッシュ更新 ({datetime.now().strftime("%Y-%m-%d %H:%M")})')
    print(f'  対象: 直近{args.days}日  上限{args.limit}頭  間隔{args.sleep}秒')
    print('=' * 50)
    update_cache(days=args.days, limit=args.limit, sleep_sec=args.sleep)
