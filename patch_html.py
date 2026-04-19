import os, re, json

WAKU_HEX = ['#FFFFFF','#222222','#D83A3A','#2E6FD1','#F5C518','#2A8A4A','#E88527','#F3A3BD']
LIGHT_COLORS = {'#FFFFFF','#F5C518','#F3A3BD'}

def get_waku_color(num_horses, umaban):
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
            if uma == umaban: return WAKU_HEX[wi]
            uma += 1
    return '#888888'

dirs = ['output/20260418', 'output/20260419']
for d in dirs:
    for f in sorted(os.listdir(d)):
        if not f.startswith('scatter_') or not f.endswith('.html'):
            continue
        path = os.path.join(d, f)
        with open(path, encoding='utf-8') as fh:
            html = fh.read()
        orig = html

        # 1. HORSES JSONを取得して枠番カラー付与＋馬番順ソート
        m = re.search(r'const HORSES = (\[.*?\]);', html, re.DOTALL)
        if m:
            horses = json.loads(m.group(1))
            total = len(horses)
            for h in horses:
                try:
                    hnum = int(h.get('horse_num', ''))
                    h['waku_color'] = get_waku_color(total, hnum)
                except (ValueError, TypeError):
                    h['waku_color'] = '#888888'
            horses.sort(key=lambda h: int(h.get('horse_num') or 999))
            new_horses_json = json.dumps(horses, ensure_ascii=False)
            html = html[:m.start(1)] + new_horses_json + html[m.end(1):]

        # 2. horseColors をwaku_colorベースに
        html = re.sub(
            r'const hueStep = .*?\n.*?const horseColors = .*?;',
            "const horseColors = HORSES.map(h => h.waku_color || '#888888');",
            html, flags=re.DOTALL
        )
        html = re.sub(
            r"const horseColors = HORSES\.map\(\(_, i\) => `hsl[^`]+`\);",
            "const horseColors = HORSES.map(h => h.waku_color || '#888888');",
            html
        )

        # 3. h-num バッジに枠番カラー適用
        old_btn = "      <span class=\"h-dot\" style=\"background:${horseColors[i]}\"></span>\n      <span class=\"h-num\">${h.horse_num}</span>"
        new_btn = "      <span class=\"h-num\" style=\"background:${numColor};color:${textColor}\">${h.horse_num}</span>"
        html = html.replace(old_btn, new_btn)

        # h-dotなしパターン
        old_btn2 = "      <span class=\"h-num\">${h.horse_num}</span>"
        if old_btn2 in html and 'numColor' not in html:
            html = html.replace(old_btn2, "      <span class=\"h-num\" style=\"background:${numColor};color:${textColor}\">${h.horse_num}</span>")

        # numColor/textColor 定義を horse-btn生成の直前に挿入
        if 'const numColor' not in html:
            html = html.replace(
                "    html += `<button class=\"horse-btn",
                "    const numColor = h.waku_color || '#888888';\n    const textColor = ['#FFFFFF','#F5C518','#F3A3BD'].includes(numColor) ? '#222' : '#fff';\n    html += `<button class=\"horse-btn"
            )

        # 4. rc-winner/rc-sub font-size
        html = html.replace('.rc-winner { font-size: 9px;', '.rc-winner { font-size: 11px;')
        html = html.replace('.rc-sub { font-size: 9px;', '.rc-sub { font-size: 11px;')

        # 5. ★→◆
        html = html.replace("ctx.strokeText('★', tx, ty); ctx.fillText('★', tx, ty);",
                            "ctx.strokeText('◆', tx, ty); ctx.fillText('◆', tx, ty);")

        # 6. race-mark-btn active
        html = html.replace('.race-mark-btn:active { transform: scale(0.9); }',
                            '.race-mark-btn:active { transform: none; }')

        # 6a. ヘッダーを1行（h1＋バッジ＋時刻）のflex行に
        tm = re.search(r'_(\d{4})(?:_G[123])?\.html$', f)
        time_str = f'{tm.group(1)[:2]}:{tm.group(1)[2:]}' if tm else ''
        # CSS
        for old_h1css, new_h1css in [
            ('.header h1 {\n  font-size: 15px; font-weight: 900; letter-spacing: -0.3px;\n'
             '  background: linear-gradient(90deg, #ffffff, #7eb8f7);\n'
             '  -webkit-background-clip: text; -webkit-text-fill-color: transparent;\n'
             '  line-height: 1.2;\n}',
             '.header-row { display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; }\n'
             '.header h1 {\n  font-size: 14px; font-weight: 900; letter-spacing: -0.3px;\n'
             '  background: linear-gradient(90deg, #ffffff, #7eb8f7);\n'
             '  -webkit-background-clip: text; -webkit-text-fill-color: transparent;\n'
             '  line-height: 1.3; white-space: nowrap;\n}\n.header-time { font-size: 11px; color: var(--text-muted); font-weight: 700; }'),
            # 前パッチ済み版
            ('.header h1 {\n  font-size: 14px; font-weight: 900; letter-spacing: -0.3px;\n'
             '  background: linear-gradient(90deg, #ffffff, #7eb8f7);\n'
             '  -webkit-background-clip: text; -webkit-text-fill-color: transparent;\n'
             '  line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;\n}',
             '.header-row { display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; }\n'
             '.header h1 {\n  font-size: 14px; font-weight: 900; letter-spacing: -0.3px;\n'
             '  background: linear-gradient(90deg, #ffffff, #7eb8f7);\n'
             '  -webkit-background-clip: text; -webkit-text-fill-color: transparent;\n'
             '  line-height: 1.3; white-space: nowrap;\n}\n.header-time { font-size: 11px; color: var(--text-muted); font-weight: 700; }'),
        ]:
            html = html.replace(old_h1css, new_h1css)
        html = re.sub(
            r'\.header-row \{ display: flex;[^}]+\}',
            '.header-row { display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; }',
            html
        )
        html = html.replace('.header .sub { font-size: 10px; color: var(--text-muted); margin-top: 2px; }',
                            '.header .sub { display: none; }')
        if '@media (max-width: 500px)' not in html:
            html = html.replace(
                '.header-time { font-size: 11px; color: var(--text-muted); font-weight: 700; }',
                '.header-time { font-size: 13px; color: var(--text-muted); font-weight: 700; flex-shrink: 0; }'
                '\n@media (max-width: 500px) {'
                '\n  .header-row { gap: 4px; }'
                '\n  .header h1 { font-size: 10px; letter-spacing: -0.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; }'
                '\n  .badge { font-size: 10px; padding: 2px 5px; min-width: 60px; }'
                '\n  .header-time { font-size: 10px; }'
                '\n  .sbadge { font-size: 10px; padding: 1px 4px; }'
                '\n}'
            )
        html = html.replace('.header-badges {\n  display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; align-items: center;\n}',
                            '.header-badges { display: none; }')
        # 馬場判定（ファイル名から）
        surf_class = 'turf' if '_芝' in f else 'dirt'

        # TX/TYからCV/含水率を取得（常に最新値）
        tx_m = re.search(r'const TX = ([\d.]+);', html)
        ty_m = re.search(r'const TY = ([\d.]+);', html)
        tx_val = tx_m.group(1) if tx_m else ''
        ty_val = ty_m.group(1) if ty_m else ''

        surf_lbl = '芝' if surf_class == 'turf' else 'ダ'

        # バッジCSS更新（sbadge追加＋馬場色）
        new_badge_css = (
            '.sbadge { font-size: 13px; font-weight: 900; padding: 2px 6px; border-radius: 6px; flex-shrink: 0; }'
            '\n.sbadge.turf { background: rgba(34,197,94,0.2); color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }'
            '\n.sbadge.dirt { background: rgba(245,158,11,0.2); color: #fbbf24; border: 1px solid rgba(245,158,11,0.4); }'
            '\n.badge { display: inline-flex; align-items: center; justify-content: center; gap: 5px;'
            ' background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.25);'
            ' backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);'
            ' padding: 3px 8px; border-radius: 6px; min-width: 78px; flex-shrink: 0;'
            ' font-size: 13px; font-weight: 700; font-family: monospace; color: #ffffff; }'
            '\n.badge b { color: #ffffff; }'
        )
        html = re.sub(
            r'(?:\.sbadge \{[^}]+\}[\s\S]*?)?\.badge \{[^}]+\}[\s\S]*?\.badge(?:\.dirt)? b \{[^}]+\}',
            new_badge_css, html
        )
        html = re.sub(r'\.badge\.(cv|moist|turf|dirt) b? ?\{[^}]+\}', '', html)

        # h1内にsbadgeを挿入、バッジからはsbadgeを除去してガラス風に統一
        # h1の「ダ1200m」や「芝2000m」をsbadge付きに変換
        html = re.sub(
            r'<h1>([^<]+?)([ダ芝障])(\d+m)</h1>',
            lambda m: f'<h1>{m.group(1)}<span class="sbadge {surf_class}" style="-webkit-text-fill-color:initial">{m.group(2)}</span>{m.group(3)}</h1>',
            html
        )
        # バッジをガラス風シンプル形式に統一（全パターン対応）
        html = re.sub(
            r'<div class="badge[^"]*"><span(?:[^>]*)>(?:ダ|芝|障)</span><span>CV <b>(.*?)</b></span></div>',
            lambda m: f'<div class="badge">CV <b>{m.group(1) or tx_val}</b></div>',
            html
        )
        html = re.sub(
            r'<div class="badge[^"]*"><span(?:[^>]*)>(?:ダ|芝|障)</span><span>含水率 <b>(.*?)</b></span></div>',
            lambda m: f'<div class="badge">含水率 <b>{m.group(1) or (ty_val+"%")}</b></div>',
            html
        )
        html = re.sub(
            r'<div class="badge[^"]*"><span>CV</span><b>(.*?)</b></div>',
            lambda m: f'<div class="badge">CV <b>{m.group(1) or tx_val}</b></div>',
            html
        )
        html = re.sub(
            r'<div class="badge[^"]*"><span>含水率</span><b>(.*?)</b></div>',
            lambda m: f'<div class="badge">含水率 <b>{m.group(1) or (ty_val+"%")}</b></div>',
            html
        )

        # HTML構造 — h1＋バッジをflex行でまとめる（header-row未適用の場合のみ）
        m_hdr = re.search(r'<div class="header">(.*?)</div>\s*\n<div class="main">', html, re.DOTALL)
        if m_hdr and 'header-row' not in m_hdr.group(1):
            inner = m_hdr.group(1)
            h1m = re.search(r'<h1>(.*?)</h1>', inner)
            if h1m:
                h1_text = h1m.group(1)
                # h1テキストからCV/含水率テキストを除去（前パッチ済みの場合）
                h1_text = re.sub(r'　CV[\d.]+　含水率[\d.]+%(?:　[\d:]+)?$', '', h1_text)
                cv_m = re.search(r'class="badge cv"[^>]*>.*?<b>(.*?)</b>', inner, re.DOTALL)
                mo_m = re.search(r'class="badge moist"[^>]*>.*?<b>(.*?)</b>', inner, re.DOTALL)
                cv_v = cv_m.group(1) if cv_m else ''
                mo_v = mo_m.group(1) if mo_m else ''
                # バッジがない場合はh1テキストから抽出
                if not cv_v:
                    cv_txt = re.search(r'CV([\d.]+)', h1m.group(1))
                    cv_v = cv_txt.group(1) if cv_txt else ''
                if not mo_v:
                    mo_txt = re.search(r'含水率([\d.]+)%', h1m.group(1))
                    mo_v = (mo_txt.group(1) + '%') if mo_txt else ''
                new_inner = (
                    f'<div class="header">\n  <div class="header-row">\n'
                    f'    <h1>{h1_text}</h1>\n'
                    f'    <div class="badge cv"><span>CV</span><b>{cv_v}</b></div>\n'
                    f'    <div class="badge moist"><span>含水率</span><b>{mo_v}</b></div>\n'
                    f'    <span class="header-time">{time_str}</span>\n'
                    f'  </div>\n</div>'
                )
                html = html.replace(m_hdr.group(0), new_inner + '\n<div class="main">', 1)

        # 6b. 着順色を散布図カラー（COLORS[r.cat]）に統一
        html = html.replace(
            "const resultColor = r.result === 1 ? '#f59e0b' : r.result !== null && r.result <= 3 ? '#22c55e' : '#ef4444';",
            "const resultColor = COLORS[r.cat] || '#888888';"
        )

        # 7. レース表示形式（レース名＋距離を1行に）
        html = html.replace(
            "const distLabel = r.distance === TDIST ? '同距離' : (r.distance > TDIST ? '短縮' : '延長');",
            "const distLabel = r.distance === TDIST ? '同' : (r.distance > TDIST ? '短' : '延');"
        )
        old_rc = ('          <div class="rc-name">${r.race_name}</div>\n'
                  '          <div class="rc-cond">${r.surface}${r.distance}m (${distLabel})</div>')
        new_rc = '          <div class="rc-name">${r.race_name}　${r.surface}${r.distance}m（${distLabel}）</div>'
        html = html.replace(old_rc, new_rc)

        # 7b. レースカード再レイアウト（日付行にCV、上がり/通過を上に、着順/勝馬を下に）
        # CSS
        html = html.replace(
            '.rc-bottom { display: flex; justify-content: space-between; align-items: center; margin-top: 3px; }',
            '.rc-agari { font-size: 11px; color: var(--text-muted); margin-top: 3px; }'
            '\n.rc-result-row { text-align: right; margin-top: 3px; }'
        )
        html = html.replace(
            '.rc-date { color: var(--text-muted); font-weight: 600; font-family: monospace; font-size: 9px; }',
            '.rc-date { color: var(--text-muted); font-weight: 600; font-family: monospace; font-size: 9px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }'
        )
        # HTML構造
        old_card = (
            '          <div class="rc-date">${r.date} ${r.venue}</div>\n'
            '          <div class="rc-name">${r.race_name}　${r.surface}${r.distance}m（${distLabel}）</div>\n'
            '          <div class="rc-bottom">\n'
            '            <span class="rc-cv">CV${r.cushion} / ${r.moisture}%</span>\n'
            '            <span class="rc-result" style="color:${resultColor}">${r.result !== null ? r.result + \'着\' : \'取消\'}</span>\n'
            '          </div>\n'
            '          ${r.winner ? `<div class="rc-winner">${r.winner}${r.time_diff ? \' (\' + r.time_diff + \')\' : \'\'}</div>` : \'\'}\n'
            '          ${r.agari ? `<div class="rc-sub">上がり ${r.agari}</div>` : \'\'}\n'
            '          ${r.passage ? `<div class="rc-sub">通過 ${r.passage}</div>` : \'\'}'
        )
        new_card = (
            '          <div class="rc-date">${r.date}　${r.venue}　CV${r.cushion}/${r.moisture}%</div>\n'
            '          <div class="rc-name">${r.race_name}　${r.surface}${r.distance}m（${distLabel}）</div>\n'
            '          <div class="rc-result-row">\n'
            '            <span class="rc-result" style="color:${resultColor}">${r.result !== null ? r.result + \'着\' : \'取消\'}</span>\n'
            '          </div>\n'
            '          ${r.winner ? `<div class="rc-winner">${r.winner}${r.time_diff ? \' (\' + r.time_diff + \')\' : \'\'}</div>` : \'\'}\n'
            '          ${(r.agari || r.passage) ? `<div class="rc-agari">${r.agari ? \'上がり \' + r.agari : \'\'}${r.agari && r.passage ? \'　\' : \'\'}${r.passage ? \'通過 \' + r.passage : \'\'}</div>` : \'\'}'
        )
        html = html.replace(old_card, new_card)

        # 7c. 上がり/通過と着順を同一行に（flex行）、勝馬は右揃えで下に
        # CSS更新
        html = html.replace(
            '.rc-agari { font-size: 11px; color: var(--text-muted); margin-top: 3px; }\n.rc-result-row { text-align: right; margin-top: 3px; }',
            '.rc-mid-row { display: flex; justify-content: space-between; align-items: center; margin-top: 3px; }\n.rc-agari { font-size: 11px; color: var(--text-muted); }'
        )
        # HTML: パターンA（rc-agari→rc-result-row→rc-winner順）
        old_orderA = (
            '          ${(r.agari || r.passage) ? `<div class="rc-agari">${r.agari ? \'上がり \' + r.agari : \'\'}${r.agari && r.passage ? \'　\' : \'\'}${r.passage ? \'通過 \' + r.passage : \'\'}</div>` : \'\'}\n'
            '          <div class="rc-result-row">\n'
            '            <span class="rc-result" style="color:${resultColor}">${r.result !== null ? r.result + \'着\' : \'取消\'}</span>\n'
            '          </div>\n'
            '          ${r.winner ? `<div class="rc-winner">${r.winner}${r.time_diff ? \' (\' + r.time_diff + \')\' : \'\'}</div>` : \'\'}'
        )
        # パターンB（rc-result-row→rc-winner→rc-agari順）
        old_orderB = (
            '          <div class="rc-result-row">\n'
            '            <span class="rc-result" style="color:${resultColor}">${r.result !== null ? r.result + \'着\' : \'取消\'}</span>\n'
            '          </div>\n'
            '          ${r.winner ? `<div class="rc-winner">${r.winner}${r.time_diff ? \' (\' + r.time_diff + \')\' : \'\'}</div>` : \'\'}\n'
            '          ${(r.agari || r.passage) ? `<div class="rc-agari">${r.agari ? \'上がり \' + r.agari : \'\'}${r.agari && r.passage ? \'　\' : \'\'}${r.passage ? \'通過 \' + r.passage : \'\'}</div>` : \'\'}'
        )
        new_mid = (
            '          <div class="rc-mid-row">\n'
            '            <span class="rc-agari">${(r.agari || r.passage) ? (r.agari ? \'上がり \' + r.agari : \'\') + (r.agari && r.passage ? \'　\' : \'\') + (r.passage ? \'通過 \' + r.passage : \'\') : \'\'}</span>\n'
            '            <span class="rc-result" style="color:${resultColor}">${r.result !== null ? r.result + \'着\' : \'取消\'}</span>\n'
            '          </div>\n'
            '          ${r.winner ? `<div class="rc-winner">${r.winner}${r.time_diff ? \' (\' + r.time_diff + \')\' : \'\'}</div>` : \'\'}'
        )
        html = html.replace(old_orderA, new_mid)
        html = html.replace(old_orderB, new_mid)

        # 9. ヘッダーCSS再構成（フォントサイズ統一＋構造修正）
        # .header-time から .badge b までを一括置換して正しい構造に毎回修正
        header_css_fixed = (
            '.header-time { font-size: 13px; color: var(--text-muted); font-weight: 700; flex-shrink: 0; }'
            '\n@media (max-width: 500px) {'
            '\n  .header-row { gap: 4px; }'
            '\n  .header h1 { font-size: 10px; letter-spacing: -0.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; }'
            '\n  .badge { font-size: 10px; padding: 2px 5px; min-width: 60px; }'
            '\n  .header-time { font-size: 10px; }'
            '\n  .sbadge { font-size: 10px; padding: 1px 4px; }'
            '\n}'
            '\n.sbadge { font-size: 13px; font-weight: 900; padding: 2px 6px; border-radius: 6px; flex-shrink: 0; }'
            '\n.sbadge.turf { background: rgba(34,197,94,0.2); color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }'
            '\n.sbadge.dirt { background: rgba(245,158,11,0.2); color: #fbbf24; border: 1px solid rgba(245,158,11,0.4); }'
            '\n.badge { display: inline-flex; align-items: center; justify-content: center; gap: 5px;'
            ' background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.25);'
            ' backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);'
            ' padding: 3px 8px; border-radius: 6px; min-width: 78px; flex-shrink: 0;'
            ' font-size: 13px; font-weight: 700; font-family: monospace; color: #ffffff; }'
            '\n.badge b { color: #ffffff; }'
        )
        html = re.sub(
            r'\.header-time \{[^}]+\}[\s\S]*?\.badge b \{[^}]+\}',
            header_css_fixed, html
        )
        # h1 font-size統一
        html = html.replace(
            '  font-size: 14px; font-weight: 900; letter-spacing: -0.3px;',
            '  font-size: 13px; font-weight: 900; letter-spacing: -0.3px;'
        )

        # 8. オッズ自動更新（60秒ごと）
        html = html.replace(
            "(async function fetchOdds() {",
            "async function fetchOdds() {"
        )
        if '})();\n</script>' in html and 'setInterval(fetchOdds' not in html:
            html = html.replace(
                '})();\n</script>',
                'fetchOdds();\nsetInterval(fetchOdds, 60000);\n</script>'
            )
        elif '}})();\n</script>' in html and 'setInterval(fetchOdds' not in html:
            html = html.replace(
                '}})();\n</script>',
                '}}fetchOdds();\nsetInterval(fetchOdds, 60000);\n</script>'
            )

        if html != orig:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(html)
            print(f'Updated: {f}')
