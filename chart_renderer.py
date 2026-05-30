"""
価格推移グラフ画像生成モジュール
=================================
matplotlib を使用してレアリティ別折れ線グラフを生成する。
GitHub Actions (ubuntu-latest) 上では apt-get で Noto CJK フォントを
インストールする（post-featured.yml 参照）。

グラフ生成関数:
    render_price_chart(card_name, history_rows, out_path=None) -> str | None

データ点が3未満の発売直後は「初値カード」画像（カード名+価格の大きな表示）に
自動でフォールバックする。
"""

import os
import tempfile
from datetime import datetime
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")  # ヘッドレス描画（GUIウィンドウ不要）
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.dates as mdates
import matplotlib.font_manager as fm

# ── 日本語フォント登録（1プロセス1回） ──

_FONT_REGISTERED = False


def _register_japanese_font():
    """
    日本語フォントを matplotlib に登録する。
    1. リポジトリ同梱 assets/fonts/NotoSansJP-Regular.otf
    2. システムフォント（apt install fonts-noto-cjk 後の Noto CJK）
    3. その他の CJK フォント
    """
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return

    # 1. リポジトリ同梱フォント
    here = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.join(here, "assets", "fonts", "NotoSansJP-Regular.otf")
    if os.path.exists(bundled):
        fm.fontManager.addfont(bundled)
        plt.rcParams["font.family"] = "Noto Sans JP"
        print(f"  [chart] フォント登録（同梱）: {bundled}")
        _FONT_REGISTERED = True
        return

    # 2. システムフォントファイルを直接探す（GitHub Actions の apt-get fonts-noto-cjk）
    system_font_paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/truetype/ipafont/ipag.ttf",
    ]
    for path in system_font_paths:
        if os.path.exists(path):
            fm.fontManager.addfont(path)
            prop = fm.FontProperties(fname=path)
            plt.rcParams["font.family"] = prop.get_name()
            print(f"  [chart] フォント登録（システム）: {path}")
            _FONT_REGISTERED = True
            return

    # 3. フォント名での検索（既にインストール済みの場合）
    for name in ["Noto Sans CJK JP", "Noto Sans JP", "IPAGothic", "IPAexGothic", "TakaoPGothic"]:
        found = fm.findfont(fm.FontProperties(family=name), fallback_to_default=False)
        if found and "DejaVu" not in found:
            plt.rcParams["font.family"] = name
            print(f"  [chart] フォント登録（ファミリー検索）: {name}")
            _FONT_REGISTERED = True
            return

    print("  [chart] 警告: 日本語フォントが見つかりません（文字化けの可能性があります）")
    _FONT_REGISTERED = True


# ── レアリティの表示優先順位と色（フロントの Chart.js と統一） ──

_RARITY_ORDER = ["シークレット", "アルティメット", "ウルトラ", "スーパー", "レア", "ノーマル"]
_RARITY_COLORS = {
    "シークレット": "#e74c3c",
    "アルティメット": "#9b59b6",
    "ウルトラ":   "#f39c12",
    "スーパー":   "#3498db",
    "レア":       "#2ecc71",
    "ノーマル":   "#95a5a6",
}


def render_price_chart(card_name: str, history_rows: list, out_path: str = None) -> str | None:
    """
    価格推移グラフ画像を生成してファイルパスを返す。
    失敗時は None を返す。

    Args:
        card_name:    カード名（グラフタイトルに使用）
        history_rows: price_history の行リスト
                      各行: {"rarity": ..., "min_price": ..., "recorded_at": ...}
        out_path:     保存先パス。None の場合は一時ファイルを作成

    Returns:
        str | None — 保存したファイルのパス
    """
    _register_japanese_font()

    # レアリティ×日付で最安値を集計
    rarity_dates: dict[str, dict[str, int]] = defaultdict(dict)
    for row in history_rows:
        rarity = (row.get("rarity") or "ノーマル")
        d_str = (row.get("recorded_at") or "")[:10]
        price = row.get("min_price") or 0
        if not d_str or price <= 10:
            continue
        if d_str not in rarity_dates[rarity] or price < rarity_dates[rarity][d_str]:
            rarity_dates[rarity][d_str] = price

    # 全日付をソート
    all_dates = sorted({d for dates in rarity_dates.values() for d in dates})

    # データ点が3未満は初値カード画像にフォールバック
    total_points = sum(len(dates) for dates in rarity_dates.values())
    if total_points < 3 or not all_dates:
        return _render_initial_card(card_name, rarity_dates, out_path)

    try:
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
        fig.patch.set_facecolor("#ffffff")

        plotted_any = False
        for rarity in _RARITY_ORDER:
            if rarity not in rarity_dates:
                continue
            dates = rarity_dates[rarity]
            xs, ys = [], []
            for d in all_dates:
                if d in dates:
                    xs.append(datetime.strptime(d, "%Y-%m-%d"))
                    ys.append(dates[d])
            if len(xs) < 2:
                continue
            color = _RARITY_COLORS.get(rarity, "#7f8c8d")
            ax.plot(xs, ys, marker="o", label=rarity, color=color, linewidth=2, markersize=5)
            plotted_any = True

        if not plotted_any:
            plt.close(fig)
            return _render_initial_card(card_name, rarity_dates, out_path)

        # 軸の書式設定
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        fig.autofmt_xdate(rotation=30)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}円"))

        ax.set_title(card_name, fontsize=13, pad=10)
        ax.legend(loc="upper left", fontsize=9, framealpha=0.7)
        ax.grid(True, alpha=0.3, linestyle="--")
        ax.set_facecolor("#f8f9fa")
        fig.tight_layout()

        out_path = _save_to_file(fig, out_path)
        print(f"  [chart] 推移グラフ生成: {card_name} → {out_path}")
        return out_path

    except Exception as e:
        print(f"  [chart] グラフ生成失敗 [{card_name}]: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def _render_initial_card(card_name: str, rarity_dates: dict, out_path: str = None) -> str | None:
    """
    データ点が少ない発売直後用。
    カード名と初値（最安値）を大きく表示した画像を生成する。
    """
    _register_japanese_font()

    # 最安値を取得
    best_price = None
    best_rarity = None
    for rarity in _RARITY_ORDER:
        if rarity not in rarity_dates:
            continue
        for price in rarity_dates[rarity].values():
            if best_price is None or price < best_price:
                best_price = price
                best_rarity = rarity

    try:
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

        # カード名
        ax.text(0.5, 0.68, card_name,
                ha="center", va="center",
                fontsize=15, color="white", fontweight="bold",
                transform=ax.transAxes)

        if best_price is not None:
            price_text = f"初値: {best_price:,}円"
            if best_rarity:
                price_text += f"  ({best_rarity})"
            ax.text(0.5, 0.44, price_text,
                    ha="center", va="center",
                    fontsize=19, color="#f1c40f", fontweight="bold",
                    transform=ax.transAxes)
        else:
            ax.text(0.5, 0.44, "価格データ収集中",
                    ha="center", va="center",
                    fontsize=14, color="#bdc3c7",
                    transform=ax.transAxes)

        ax.text(0.5, 0.22, "価格推移は数日後から表示されます",
                ha="center", va="center",
                fontsize=9, color="#7f8c8d",
                transform=ax.transAxes)

        fig.tight_layout()
        out_path = _save_to_file(fig, out_path)
        print(f"  [chart] 初値カード画像生成: {card_name} → {out_path}")
        return out_path

    except Exception as e:
        print(f"  [chart] 初値カード画像生成失敗 [{card_name}]: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def _save_to_file(fig, out_path: str = None) -> str:
    """fig を out_path に保存し、パスを返す。out_path が None なら一時ファイルを作成。"""
    if out_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        out_path = tmp.name
        tmp.close()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out_path
