"""
過去走ポジショニングカード生成 (HTML + Playwright方式)
Usage: python sns/make_positioning_card.py <scatter_html> <horse_name> [out_path]
"""
import sys, re, json, os, tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR  = os.path.join(BASE_DIR, 'output', 'sns')
os.makedirs(OUT_DIR, exist_ok=True)

WAKU_PALETTE = [
    {'bg': '#FFFFFF', 'tc': '#111111'},
    {'bg': '#1A1A1A', 'tc': '#ffffff'},
    {'bg': '#D83A3A', 'tc': '#ffffff'},
    {'bg': '#2E6FD1', 'tc': '#ffffff'},
    {'bg': '#F5C518', 'tc': '#111111'},
    {'bg': '#2A8A4A', 'tc': '#ffffff'},
    {'bg': '#E88527', 'tc': '#111111'},
    {'bg': '#F3A3BD', 'tc': '#111111'},
]

CAT_COLORS = {
    'same_dist':    '#f87171',
    'diff_dist':    '#60a5fa',
    'diff_surface': '#cbd5e1',
    'cancel':       '#888888',
}
CAT_TC = {
    'same_dist':    'white',
    'diff_dist':    'white',
    'diff_surface': '#333',
    'cancel':       'white',
}


def _waku_dist(n):
    if n <= 8:
        return [1 if i < n else 0 for i in range(8)]
    if n <= 16:
        a = [1] * 8
        for i in range(n - 8):
            a[7 - i] = 2
        return a
    if n == 17:
        return [2, 2, 2, 2, 2, 2, 2, 3]
    return [2, 2, 2, 2, 2, 2, 3, 3]


def get_waku_color(num_horses, umaban):
    dist = _waku_dist(num_horses)
    uma = 1
    for w in range(8):
        for _ in range(dist[w]):
            if uma == umaban:
                return WAKU_PALETTE[w]
            uma += 1
    return WAKU_PALETTE[1]


def _split_race_name(name):
    """レース名とグレード（）を分離して返す: (base, grade)"""
    m = re.search(r'([（(][^）)]+[）)])$', name.strip())
    grade = m.group(1) if m else ''
    base  = name[:m.start()].strip() if m else name.strip()
    return base, grade


def _rname_html(name, base_cls, grade_cls):
    """レース名をbase+gradeのspan2つに分けたHTMLを返す"""
    base, grade = _split_race_name(name)
    g = f'<span class="{grade_cls}">{grade}</span>' if grade else ''
    return f'<span class="{base_cls}">{base}</span>{g}'


def parse_scatter(html_path):
    with open(html_path, encoding='utf-8') as f:
        content = f.read()
    tx  = float(re.search(r'const TX\s*=\s*([\d.]+)', content).group(1))
    ty  = float(re.search(r'const TY\s*=\s*([\d.]+)', content).group(1))
    hm  = re.search(r'const HORSES\s*=\s*(\[.*?\]);', content, re.DOTALL)
    horses = json.loads(hm.group(1)) if hm else []
    return tx, ty, horses


def _race_header(scatter_html):
    parts = os.path.splitext(os.path.basename(scatter_html))[0].split('_')
    venue_r = parts[2] if len(parts) > 2 else ''
    race_n  = parts[3] if len(parts) > 3 else ''
    course  = parts[4] if len(parts) > 4 else ''
    return venue_r, race_n, course


def _to_pct(val, vmin, vmax):
    if vmax == vmin:
        return 50.0
    return (val - vmin) / (vmax - vmin) * 100.0


def make_card(scatter_html, horse_name, out_path=None):
    tx, ty, horses = parse_scatter(scatter_html)

    horse = next((h for h in horses if h['name'] == horse_name), None)
    if horse is None:
        raise ValueError(f'Horse not found: {horse_name}')

    races     = horse['races']
    horse_num = horse.get('horse_num', '?')
    venue_r, race_name, course_str = _race_header(scatter_html)

    # 枠色（馬番バッジ）
    try:
        umaban = int(horse_num)
    except (ValueError, TypeError):
        umaban = 1
    waku = get_waku_color(len(horses), umaban)
    num_bg = waku['bg']
    num_tc = waku['tc']

    # ★は中央固定
    star_left = 50.0
    star_top  = 50.0

    # 散布図のX:Y比率 (cushion 5単位 : moisture 22単位) を維持しつつ
    # 最遠カードが中心から±40%になるよう自動スケール
    SCALE_RATIO = 22.0 / 5.0   # scale_c = SCALE_RATIO * scale_m
    max_c = max((abs(r['cushion'] - tx) for r in races), default=0.5) + 0.5
    max_m = max((abs(r['moisture'] - ty) for r in races), default=0.5) + 0.5
    scale_m = min(40.0 / (SCALE_RATIO * max_c), 40.0 / max_m)
    scale_c = scale_m * SCALE_RATIO

    cards_html = ''
    for i, r in enumerate(races):
        left_pct = 50.0 + (r['cushion'] - tx) * scale_c
        top_pct  = 50.0 - (r['moisture'] - ty) * scale_m   # Y軸反転

        result = r.get('result')
        cat    = r.get('cat', 'diff_dist') if result is not None else 'cancel'
        border = CAT_COLORS.get(cat, '#60a5fa')
        pos_bg = border
        pos_tc = CAT_TC.get(cat, 'white')
        pos_txt = f'{result}着' if result is not None else '取消'

        # 1着・2着は前面
        z = (15 if result is not None and result <= 2 else 10) + (len(races) - i)

        rname_html = _rname_html(r['race_name'], 'rn-base', 'rn-grade')

        cards_html += f'''
      <div class="race-card" style="left:{left_pct:.1f}%;top:{top_pct:.1f}%;z-index:{z};border-color:{border};">
        <div class="date">{r['date']}</div>
        <div class="rname">{rname_html}</div>
        <div class="result">
          <span class="course">{r['venue']} {r['distance']}m</span>
          <span class="pos" style="background:{pos_bg};color:{pos_tc};">{pos_txt}</span>
        </div>
      </div>'''

    html = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Yu Gothic", "Meiryo", "Hiragino Kaku Gothic ProN", sans-serif;
    background: #666;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
  }}
  .app {{
    width: 1200px;
    height: 675px;
    background: #1e2b4a;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }}
  .app-header {{
    padding: 16px 28px 6px;
    background: #1a2035;
  }}
  .race-title {{
    color: white;
    font-size: 22px;
    font-weight: bold;
  }}
  .app-horse-row {{
    padding: 10px 28px;
    background: #243554;
    display: flex;
    align-items: center;
  }}
  .app-horse {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
  }}
  .app-horse .num {{
    padding: 5px 12px;
    border-radius: 5px;
    font-size: 16px;
    font-weight: bold;
  }}
  .app-horse .name {{
    color: white;
    font-size: 20px;
    font-weight: bold;
  }}
  .chart {{
    flex: 1;
    background: #1e2b4a;
    padding: 10px;
    position: relative;
  }}
  .plot {{
    position: absolute;
    inset: 10px;
    isolation: isolate;
  }}
  .grid-bg {{
    position: absolute;
    inset: 0;
    background:
      linear-gradient(to right, rgba(143,163,212,0.06) 1px, transparent 1px) 0 0 / calc(100%/14) 100%,
      linear-gradient(to bottom, rgba(143,163,212,0.06) 1px, transparent 1px) 0 0 / 100% calc(100%/8);
  }}
  .current-race {{
    position: absolute;
    transform: translate(-50%, -50%);
    z-index: 30;
    left: {star_left:.1f}%;
    top: {star_top:.1f}%;
  }}
  .current-race .star {{
    font-size: 56px;
    color: #ffd43b;
    filter: drop-shadow(0 0 16px rgba(255,212,59,1));
    line-height: 1;
  }}
  .race-card {{
    position: absolute;
    transform: translate(-50%, -50%);
    background: rgba(26, 82, 118, 0.72);
    mix-blend-mode: normal;
    border: 2px solid;
    border-radius: 10px;
    padding: 12px 16px;
    width: 220px;
    height: 100px;
    overflow: hidden;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  }}
  .race-card .date,
  .race-card .rname,
  .race-card .course {{
    text-shadow: 0 1px 5px rgba(0,0,0,1);
  }}
  .race-card .date {{
    font-size: 12px;
    color: #8fa3d4;
    margin-bottom: 4px;
  }}
  .race-card .rname {{
    display: flex; align-items: baseline; gap: 5px;
    margin-bottom: 6px; font-size: 16px; color: white; font-weight: bold; line-height: 1.2;
  }}
  .race-card .rname .rn-base {{
    flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .race-card .rname .rn-grade {{ flex-shrink: 0; font-size: 14px; }}
  .race-card .result {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }}
  .race-card .course {{
    font-size: 12px;
    color: #aab;
  }}
  .race-card .pos {{
    font-size: 18px;
    font-weight: 900;
    padding: 2px 10px;
    border-radius: 5px;
  }}
</style>
</head>
<body>
<div class="app" id="capture">
  <div class="app-header">
    <div class="race-title">{venue_r} {race_name} <span style="font-size:14px;color:#8fa3d4;font-weight:normal;">{course_str}</span></div>
  </div>
  <div class="app-horse-row">
    <div class="app-horse">
      <span class="num" style="background:{num_bg};color:{num_tc};">{horse_num}</span>
      <span class="name">{horse_name}</span>
      <span style="margin-left:auto;color:#8fa3d4;font-size:13px;">好走パターン分析</span>
    </div>
  </div>
  <div class="chart">
    <div class="plot">
      <div class="grid-bg"></div>
{cards_html}
      <div class="current-race">
        <div class="star">★</div>
      </div>
    </div>
  </div>
</div>
</body>
</html>'''

    tmp = tempfile.NamedTemporaryFile(suffix='.html', delete=False, mode='w', encoding='utf-8')
    tmp.write(html)
    tmp.close()

    if out_path is None:
        base = os.path.splitext(os.path.basename(scatter_html))[0]
        out_path = os.path.join(OUT_DIR, f'positioning_{base}_{horse_name}.png')

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                viewport={'width': 1200, 'height': 675},
                device_scale_factor=2,
                locale='ja-JP'
            )
            page = ctx.new_page()
            page.goto(f'file:///{tmp.name.replace(os.sep, "/")}')
            page.wait_for_load_state('networkidle')
            page.locator('#capture').screenshot(path=out_path)
            browser.close()
    finally:
        os.unlink(tmp.name)

    return out_path


def _build_cell(tx, ty, horses, horse_name, star_size=36):
    """1頭分のセルHTML（grid用）を返す"""
    horse = next((h for h in horses if h['name'] == horse_name), None)
    if horse is None:
        return '', '#888', 'white'

    races = horse['races']
    try:
        umaban = int(horse.get('horse_num', 1))
    except (ValueError, TypeError):
        umaban = 1
    waku   = get_waku_color(len(horses), umaban)

    # スケール計算（make_card と同一ロジック）
    SCALE_RATIO = 22.0 / 5.0
    max_c = max((abs(r['cushion'] - tx) for r in races), default=0.5) + 0.5
    max_m = max((abs(r['moisture'] - ty) for r in races), default=0.5) + 0.5
    scale_m = min(40.0 / (SCALE_RATIO * max_c), 40.0 / max_m)
    scale_c = scale_m * SCALE_RATIO

    cards = ''
    for i, r in enumerate(races):
        lp = 50.0 + (r['cushion'] - tx) * scale_c
        tp = 50.0 - (r['moisture'] - ty) * scale_m
        result = r.get('result')
        cat    = r.get('cat', 'diff_dist') if result is not None else 'cancel'
        bc     = CAT_COLORS.get(cat, '#60a5fa')
        ptc    = CAT_TC.get(cat, 'white')
        ptxt   = f'{result}着' if result is not None else '取消'
        z      = (15 if result is not None and result <= 2 else 10) + (len(races) - i)
        rname_html = _rname_html(r['race_name'], 'rn-base', 'rn-grade')
        cards += f'''<div class="rc" style="left:{lp:.1f}%;top:{tp:.1f}%;z-index:{z};border-color:{bc};">
          <div class="rc-date">{r['date']}</div>
          <div class="rc-name">{rname_html}</div>
          <div class="rc-foot"><span class="rc-crs">{r['venue']} {r['distance']}m</span>
            <span class="rc-pos" style="background:{bc};color:{ptc};">{ptxt}</span></div></div>'''

    cell_html = f'''<div class="cell">
  <div class="cell-horse">
    <span class="cell-num" style="background:{waku['bg']};color:{waku['tc']};">{horse.get('horse_num','?')}</span>
    <span class="cell-hname">{horse_name}</span>
  </div>
  <div class="cell-chart">
    <div class="cell-plot">
      <div class="grid-bg"></div>
      {cards}
      <div class="cur" style="left:50%;top:50%;"><div class="star" style="font-size:{star_size}px;">★</div></div>
    </div>
  </div>
</div>'''
    return cell_html


def make_grid_card(scatter_html, horse_names, out_path=None):
    """2〜4頭を2×2グリッドで1枚に収める"""
    tx, ty, horses = parse_scatter(scatter_html)
    venue_r, race_name, course_str = _race_header(scatter_html)

    n = len(horse_names)
    cols = 2
    rows = (n + 1) // 2

    cells_html = ''.join(_build_cell(tx, ty, horses, name) for name in horse_names)
    # 奇数頭のとき空きセルを追加
    if n % 2 == 1:
        cells_html += '<div class="cell cell-empty"></div>'

    html = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Yu Gothic", "Meiryo", "Hiragino Kaku Gothic ProN", sans-serif;
    background: #666;
    display: flex; justify-content: center; align-items: center; min-height: 100vh;
  }}
  .app {{ width: 1200px; height: 675px; background: #1e2b4a; display: flex; flex-direction: column; overflow: hidden; }}
  .shared-header {{ padding: 10px 24px 8px; background: #1a2035; flex-shrink: 0; }}
  .race-title {{ color: white; font-size: 18px; font-weight: bold; }}
  .grid-wrap {{ display: grid; grid-template-columns: repeat({cols}, 1fr); gap: 2px; background: #1e2b4a; align-content: start; }}
  .cell {{ background: #1e2b4a; display: flex; flex-direction: column; overflow: hidden; height: 310px; }}
  .cell-empty {{ background: #2d4a68; }}
  .cell-horse {{ padding: 5px 12px; background: #243554; display: flex; align-items: center; gap: 8px; flex-shrink: 0; }}
  .cell-num {{ padding: 2px 8px; border-radius: 4px; font-size: 13px; font-weight: bold; }}
  .cell-hname {{ color: white; font-size: 14px; font-weight: bold; }}
  .cell-chart {{ flex: 1; position: relative; }}
  .cell-plot {{ position: absolute; inset: 6px; isolation: isolate; }}
  .grid-bg {{ position: absolute; inset: 0;
    background: linear-gradient(to right, rgba(143,163,212,0.06) 1px, transparent 1px) 0 0 / calc(100%/14) 100%,
                linear-gradient(to bottom, rgba(143,163,212,0.06) 1px, transparent 1px) 0 0 / 100% calc(100%/8); }}
  .cur {{ position: absolute; transform: translate(-50%,-50%); z-index: 30; }}
  .star {{ color: #ffd43b; filter: drop-shadow(0 0 10px rgba(255,212,59,1)); line-height: 1; }}
  .rc {{ position: absolute; transform: translate(-50%,-50%); background: rgba(26,82,118,0.72);
         mix-blend-mode: normal; border: 2px solid; border-radius: 8px; padding: 6px 10px;
         width: 155px; height: 72px; overflow: hidden; box-shadow: 0 3px 10px rgba(0,0,0,0.5); }}
  .rc-date {{ font-size: 9px; color: #8fa3d4; margin-bottom: 2px; text-shadow: 0 1px 4px rgba(0,0,0,1); }}
  .rc-name {{ display: flex; align-items: baseline; gap: 3px; margin-bottom: 4px; line-height: 1.2; text-shadow: 0 1px 4px rgba(0,0,0,1); font-size: 12px; color: white; font-weight: bold; }}
  .rc-name .rn-base {{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .rc-name .rn-grade {{ flex-shrink: 0; font-size: 10px; }}
  .rc-foot {{ display: flex; align-items: center; justify-content: space-between; gap: 4px; }}
  .rc-crs {{ font-size: 9px; color: #aab; text-shadow: 0 1px 4px rgba(0,0,0,1); }}
  .rc-pos {{ font-size: 13px; font-weight: 900; padding: 1px 7px; border-radius: 4px; }}
</style>
</head>
<body>
<div class="app" id="capture">
  <div class="shared-header">
    <div class="race-title">{venue_r} {race_name} <span style="font-size:12px;color:#8fa3d4;font-weight:normal;">{course_str}</span></div>
  </div>
  <div class="grid-wrap">
    {cells_html}
  </div>
</div>
</body>
</html>'''

    tmp = tempfile.NamedTemporaryFile(suffix='.html', delete=False, mode='w', encoding='utf-8')
    tmp.write(html)
    tmp.close()

    if out_path is None:
        base   = os.path.splitext(os.path.basename(scatter_html))[0]
        names  = '_'.join(horse_names)
        out_path = os.path.join(OUT_DIR, f'grid_{base}_{names}.png')

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                viewport={'width': 1200, 'height': 675},
                device_scale_factor=2,
                locale='ja-JP'
            )
            page = ctx.new_page()
            page.goto(f'file:///{tmp.name.replace(os.sep, "/")}')
            page.wait_for_load_state('networkidle')
            page.locator('#capture').screenshot(path=out_path)
            browser.close()
    finally:
        os.unlink(tmp.name)

    return out_path


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('Usage: python sns/make_positioning_card.py <scatter_html> <horse_name> [out]')
        sys.exit(1)
    p = make_card(sys.argv[1], sys.argv[2],
                  sys.argv[3] if len(sys.argv) > 3 else None)
    print(f'Saved: {p}')
