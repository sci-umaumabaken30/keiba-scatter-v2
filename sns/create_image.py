"""
SNS部: 投稿用画像を生成
  - SCI指数カードイメージ（単体投稿用）
  - 推奨馬券カード
  - 分析サマリーカード
出力先: output/sns/
"""

import os
import sys
import io
import textwrap

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
from datetime import date

# matplotlib 日本語対応
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib import rcParams

# 日本語フォント（Windows）
rcParams['font.family'] = ['Yu Gothic', 'Meiryo', 'MS Gothic', 'DejaVu Sans']

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE_DIR, 'output', 'sns')
os.makedirs(OUT_DIR, exist_ok=True)

BRAND_COLOR   = '#1a1a2e'   # 紺
ACCENT_COLOR  = '#e94560'   # 赤
GOLD_COLOR    = '#f5a623'   # ゴールド
TEXT_COLOR    = '#ffffff'
SUBTEXT_COLOR = '#aaaacc'


# ---------- 共通: 背景グラデーション風 ----------

def _base_fig(w=8, h=4.5):
    fig, ax = plt.subplots(figsize=(w, h))
    fig.patch.set_facecolor(BRAND_COLOR)
    ax.set_facecolor(BRAND_COLOR)
    ax.axis('off')
    return fig, ax


def _add_header(ax, title: str, subtitle: str = ''):
    ax.text(0.5, 0.93, 'SCI 走破コンフォート指数', transform=ax.transAxes,
            color=SUBTEXT_COLOR, fontsize=9, ha='center', va='top')
    ax.text(0.5, 0.85, title, transform=ax.transAxes,
            color=GOLD_COLOR, fontsize=20, fontweight='bold', ha='center', va='top')
    if subtitle:
        ax.text(0.5, 0.73, subtitle, transform=ax.transAxes,
                color=TEXT_COLOR, fontsize=11, ha='center', va='top')


def _add_footer(ax, today: str = ''):
    today = today or date.today().strftime('%Y/%m/%d')
    ax.text(0.5, 0.03, f'#{today}  #SCI指数  #競馬予想  #JRA',
            transform=ax.transAxes, color=SUBTEXT_COLOR, fontsize=8, ha='center', va='bottom')
    ax.plot([0.05, 0.95], [0.07, 0.07], transform=ax.transAxes,
            color=ACCENT_COLOR, linewidth=1.5)


# ---------- 1. SCI指数カード ----------

def make_sci_card(venue: str, race_date: str, surface: str,
                  cushion: float | None, moisture: float | None,
                  track_condition: str,
                  output_name: str = 'sci_card.png'):
    fig, ax = _base_fig()

    _add_header(ax, f'{venue}  {surface}', f'{race_date}  馬場コンディション')

    # 数値ブロック
    def _val_block(x, label, value, unit='', color=TEXT_COLOR):
        ax.text(x, 0.55, label, transform=ax.transAxes,
                color=SUBTEXT_COLOR, fontsize=10, ha='center', va='center')
        ax.text(x, 0.38, value, transform=ax.transAxes,
                color=color, fontsize=30, fontweight='bold', ha='center', va='center')
        ax.text(x, 0.24, unit, transform=ax.transAxes,
                color=SUBTEXT_COLOR, fontsize=9, ha='center', va='center')

    cv_str = f'{cushion:.1f}' if cushion is not None else '---'
    mr_str = f'{moisture:.1f}' if moisture is not None else '---'

    _val_block(0.25, 'クッション値', cv_str, '', GOLD_COLOR)
    _val_block(0.55, '含水率', mr_str, '%', '#4fc3f7')
    _val_block(0.80, '馬場状態', track_condition or '---', '', ACCENT_COLOR)

    _add_footer(ax, race_date)

    path = os.path.join(OUT_DIR, output_name)
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor=BRAND_COLOR)
    plt.close(fig)
    print(f'✅ SCI指数カード: {path}')
    return path


# ---------- 2. 推奨馬券カード ----------

def make_bet_card(venue: str, race_name: str, race_date: str,
                  picks: list[dict],
                  output_name: str = 'bet_card.png'):
    """
    picks: [{'type': '本命', 'umaban': '7', 'horse': 'フジノライト', 'sci': 82}, ...]
    """
    fig, ax = _base_fig(w=8, h=5)

    _add_header(ax, f'{venue}  {race_name}', f'{race_date}  SCI推奨馬券')

    y_start = 0.65
    for i, p in enumerate(picks[:4]):
        y = y_start - i * 0.14
        color = GOLD_COLOR if p.get('type') == '本命' else TEXT_COLOR
        label = f"【{p.get('type','推奨')}】 {p.get('umaban','?')}番  {p.get('horse','---')}"
        sci_label = f"SCIポイント: {p.get('sci', '---')}"
        ax.text(0.12, y, label,   transform=ax.transAxes, color=color,      fontsize=13, va='center')
        ax.text(0.75, y, sci_label, transform=ax.transAxes, color=SUBTEXT_COLOR, fontsize=10, va='center')

    _add_footer(ax, race_date)

    path = os.path.join(OUT_DIR, output_name)
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor=BRAND_COLOR)
    plt.close(fig)
    print(f'✅ 推奨馬券カード: {path}')
    return path


# ---------- 3. 分析サマリーカード ----------

def make_analysis_summary_card(
    title: str,
    bullet_points: list[str],
    race_date: str = '',
    output_name: str = 'analysis_summary.png'
):
    fig, ax = _base_fig(w=8, h=5)
    _add_header(ax, title)

    y = 0.67
    for point in bullet_points[:5]:
        wrapped = textwrap.shorten(point, width=40)
        ax.text(0.08, y, f'▶ {wrapped}', transform=ax.transAxes,
                color=TEXT_COLOR, fontsize=11, va='center')
        y -= 0.13

    _add_footer(ax, race_date)
    path = os.path.join(OUT_DIR, output_name)
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor=BRAND_COLOR)
    plt.close(fig)
    print(f'✅ 分析サマリーカード: {path}')
    return path


# ---------- 4. 投稿テンプレート一覧 ----------

TEMPLATES = {
    'sci_announce': """\
【{venue} {race_name}】
クッション値: {cushion} / 含水率: {moisture}%
SCI本命: {umaban}番 {horse_name}（SCIポイント{sci}）
▶ 詳細はプロフのリンクから
#{race_date} #{venue} #SCI指数 #競馬予想""",

    'result_report': """\
【{venue} {race_name} 結果】
🥇{rank1} 🥈{rank2} 🥉{rank3}
単勝: ¥{tansho} / 3連複: ¥{sanrenfuku}
SCI的中率: 今週{hit_rate}% 🎯
#SCI指数 #競馬結果 #{venue} #JRA""",

    'cushion_alert': """\
⚠️ 本日{venue}の馬場速報
クッション値 {cushion}（{condition}）
含水率 {moisture}%  天気: {weather}
この馬場でのSCI傾向 → {tendency}
#馬場速報 #SCI指数 #{venue} #競馬""",

    'weekly_summary': """\
📊 今週のSCI的中まとめ
的中数: {hit}/{total}レース
最高配当: ¥{max_payout}（{race_name}）
来週の注目: {next_focus}
#SCI指数 #競馬予想 #週間まとめ""",
}


def print_templates():
    print('\n📣 SNS部: 投稿テンプレート一覧\n')
    print('=' * 60)
    for key, tmpl in TEMPLATES.items():
        print(f'\n【{key}】')
        print(tmpl)
        print('-' * 40)


# ---------- デモ実行 ----------

def make_demo_images():
    today = date.today().strftime('%Y/%m/%d')

    make_sci_card(
        venue='東京', race_date=today, surface='芝',
        cushion=9.2, moisture=11.5, track_condition='良',
        output_name='demo_sci_card.png'
    )

    make_bet_card(
        venue='東京', race_name='天皇賞（春）', race_date=today,
        picks=[
            {'type': '本命', 'umaban': '7', 'horse': 'フジノライト', 'sci': 87},
            {'type': '対抗', 'umaban': '3', 'horse': 'カワカミジェット', 'sci': 79},
            {'type': '穴',   'umaban': '11', 'horse': 'ダイワクレッセント', 'sci': 68},
        ],
        output_name='demo_bet_card.png'
    )

    make_analysis_summary_card(
        title='好走馬の特徴（2026年1月〜）',
        bullet_points=[
            'クッション値9.0以上で先行馬の好走率UP',
            '含水率10%超でダート馬の後方差し有効',
            '6番人気以下の波乱率 28%（要注意）',
            '芝1600m・2000mの人気馬信頼度高',
            '雨天時は内枠先行有利のバイアス強い',
        ],
        race_date=today,
        output_name='demo_analysis_summary.png'
    )


if __name__ == '__main__':
    print_templates()
    print('\n--- デモ画像を生成します ---')
    make_demo_images()
    print(f'\n出力先: {OUT_DIR}')
