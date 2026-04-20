#!/usr/bin/env python3
"""
JRAライブページを監視し、新しいクッション値が公開されたら
自動でDB更新 → 散布図再生成 → デプロイを実行する。

実行タイミング (Windowsタスクスケジューラで毎30分):
  金曜: 11:00〜14:00
  土曜・日曜: 7:00〜16:00
"""

import os, sys, json, re, time, logging, subprocess
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CUSHION_DB_PATH = os.path.join(BASE_DIR, 'cushion_db_full.json')
LOG_PATH = os.path.join(BASE_DIR, 'auto_update.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


def fetch_live_cushion_keys():
    """JRAライブページから現在公開中の (venue, date_str) キーセットを返す"""
    keys = set()
    year = datetime.now().year
    try:
        r = requests.get('https://www.jra.go.jp/keiba/baba/_data_cushion.html',
                         headers=HEADERS, timeout=15)
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
                cushion_text = cushion_div.get_text(strip=True)
                if not cushion_text:
                    continue
                time_text = time_div.get_text(strip=True)
                m = re.match(r'(\d{1,2})月(\d{1,2})日', time_text)
                if m:
                    date_str = f"{year}/{int(m.group(1)):02d}/{int(m.group(2)):02d}"
                    try:
                        if float(cushion_text) > 0:
                            keys.add(f"{date_str}_{venue}")
                    except ValueError:
                        pass
    except Exception as e:
        log.warning(f"ライブページ取得エラー: {e}")
    return keys


def load_db_keys():
    """現在のDBにあるキーセットを返す"""
    if not os.path.exists(CUSHION_DB_PATH):
        return set()
    with open(CUSHION_DB_PATH, encoding='utf-8') as f:
        db = json.load(f)
    return set(db.keys())


def run_db_update():
    """update_cushion_db.py を実行"""
    log.info("DB更新開始")
    cmd = [sys.executable, '-X', 'utf8',
           os.path.join(BASE_DIR, 'update_cushion_db.py')]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', cwd=BASE_DIR)
    for line in result.stdout.splitlines():
        log.info(f"  {line}")
    if result.returncode != 0:
        log.error("DB更新失敗")
        return False
    log.info("DB更新完了")
    return True


def run_pipeline(date_yyyymmdd):
    """pipeline.py を --no-scrape --deploy 付きで実行（キャッシュ再利用で高速）"""
    log.info(f"パイプライン実行: {date_yyyymmdd}")
    cmd = [sys.executable, '-u', '-X', 'utf8',
           os.path.join(BASE_DIR, 'pipeline.py'),
           date_yyyymmdd, '--no-scrape', '--deploy']
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', cwd=BASE_DIR)
    for line in result.stdout.splitlines():
        log.info(f"  {line}")
    if result.returncode != 0:
        log.error(f"パイプライン失敗: {date_yyyymmdd}")
        return False
    log.info(f"パイプライン完了: {date_yyyymmdd}")
    return True


def affected_dates(new_keys):
    """新規キーから影響を受ける日付 (YYYYMMDD) を抽出"""
    dates = set()
    for key in new_keys:
        m = re.match(r'(\d{4})/(\d{2})/(\d{2})', key)
        if m:
            dates.add(f"{m.group(1)}{m.group(2)}{m.group(3)}")
    return sorted(dates)


def main():
    today = date.today()
    weekday = today.weekday()  # 0=月, 4=金, 5=土, 6=日
    hour = datetime.now().hour

    # 金土日以外はスキップ
    if weekday not in (4, 5, 6):
        log.info(f"本日({today}, {['月','火','水','木','金','土','日'][weekday]})は対象外")
        return

    # 時間帯チェック: 金曜は11〜14時、土日は7〜16時
    if weekday == 4 and not (11 <= hour <= 14):
        log.info(f"金曜の監視時間外 (現在{hour}時)")
        return
    if weekday in (5, 6) and not (7 <= hour <= 16):
        log.info(f"土日の監視時間外 (現在{hour}時)")
        return

    log.info(f"=== 自動更新チェック開始 ({today} {['月','火','水','木','金','土','日'][weekday]}) ===")

    live_keys = fetch_live_cushion_keys()
    db_keys = load_db_keys()
    new_keys = live_keys - db_keys

    if not new_keys:
        log.info("新データなし (DB最新)")
        return

    log.info(f"新データ検出: {len(new_keys)}件 → {sorted(new_keys)}")

    if not run_db_update():
        return

    for d in affected_dates(new_keys):
        run_pipeline(d)

    log.info("=== 自動更新完了 ===")


if __name__ == '__main__':
    main()
