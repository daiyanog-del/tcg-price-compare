"""
rarity.py — レアリティ定義の Single Source of Truth

全レアリティの「正規名・別名・色・表示順・CSSスラグ」を1箇所で管理する。
ここだけ編集すれば、収集・グラフ・バッジ・フィルタ・通知すべてに反映される。

新しいレアリティを追加したいときは RARITIES リストに1エントリ足すだけ。
エントリの形式:
  canonical : 正規名（DBに保存される代表文字列）
  slug      : ASCII のみ（CSSクラス名 rb-{slug} に使用）
  order     : 表示順序（小さいほど前に表示。10刻みの疎番で記述）
  color     : 価格推移グラフの線色（HEX）
  aliases   : この正規名に変換される全ての別名・略号・表記ゆれ

注: aliasesにはcanonical自身は含めなくてよい（起動時に自動登録される）。
    各aliasは1つのcanonicalにしか所属できない（起動時バリデーションで検出）。
"""

from __future__ import annotations
import logging
import warnings

logger = logging.getLogger(__name__)

# ── 正準定義リスト ───────────────────────────────────────────────
# order は10刻みで管理（将来の挿入を容易にするため）
# 色はフロント(JS)の RARITY_COLORS を正として統一
RARITIES: list[dict] = [
    # ── 高レアリティ ──
    {
        "canonical": "25thシークレット",
        "slug": "qcsecret",
        "order": 10,
        "color": "#9b59b6",
        "aliases": [
            "QCSE", "QC",                                       # 略号
            "クォーターセンチュリー", "QCシク",                  # 短縮・通称
            "25thシク",                                         # 短縮
            "クォーターセンチュリーシークレット",                # フル表記
            "クォーターセンチュリーシークレットレア",            # 〜レア形式
        ],
    },
    {
        "canonical": "プリシク",
        "slug": "prismatic-secret",
        "order": 20,
        "color": "#ff6b9d",
        "aliases": [
            "PSE",                                              # 略号
            "プリズマティック", "プリズマ",                     # 短縮・通称
            "プリズマティックシークレット",                     # フル表記
            "プリズマティックシークレットレア",                 # 〜レア形式
        ],
    },
    {
        "canonical": "10000シークレット",
        "slug": "10000secret",
        "order": 30,
        "color": "#ff4500",
        "aliases": [
            "10000SE",
        ],
    },
    {
        "canonical": "シークレット",
        "slug": "secret",
        "order": 40,
        "color": "#e74c3c",
        "aliases": [
            "SE",                                               # 略号
            "シク",                                             # 短縮
            "シークレットレア",                                 # 〜レア形式
        ],
    },
    {
        "canonical": "シークレットパラレル",
        "slug": "secret-parallel",
        "order": 45,
        "color": "#ff6b6b",
        "aliases": [
            "P-SE",                                             # パラレルシークレット略号（P-SR/P-UR/P-N と整合）
            "シークレットパラレルレア",                         # 〜レア形式
        ],
    },
    {
        "canonical": "EXシークレット",
        "slug": "exsecret",
        "order": 50,
        "color": "#2ecc71",
        "aliases": [
            "EXSE", "ESE", "EX",                               # 略号
            "エクシク",                                         # 短縮
            "エクストラシークレット",                           # フル表記
            "エクストラシークレットレア",                       # 〜レア形式
        ],
    },
    {
        "canonical": "アジアシークレット",
        "slug": "asia-secret",
        "order": 55,
        "color": "#c0392b",
        "aliases": [
            "AgSE",
        ],
    },
    {
        "canonical": "20thシークレット",
        "slug": "20thsecret",
        "order": 60,
        "color": "#d63384",
        "aliases": [
            "20thSE",                                           # 略号
            "20thシク",                                         # 短縮
            "20thシークレットレア",                             # 〜レア形式
        ],
    },
    {
        "canonical": "ゴールドシークレット",
        "slug": "gold-secret",
        "order": 70,
        "color": "#b8860b",
        "aliases": [
            "GSE", "GS",                                        # 略号
            "ゴルシク",                                         # 短縮
            "ゴールドシークレットレア",                         # 〜レア形式
        ],
    },
    {
        "canonical": "OFプリシク",
        "slug": "of-prismatic",
        "order": 75,
        "color": "#ff1493",
        "aliases": [
            "O-PSE",                                            # 略号（まんぞく屋/駿河屋）
            "OFプリズマティックシークレット",                   # フル表記
            "OFプリズマティックシークレットレア",               # 〜レア形式
            "オーバーフレームプリシク",                         # 別フル表記
            "オーバーフレームプリズマティックシークレット",     # 別フル表記
            "オーバーフレームプリズマティックシークレットレア", # 別フル〜レア形式
        ],
    },
    {
        "canonical": "OFシークレット",
        "slug": "of-secret",
        "order": 76,
        "color": "#dc143c",
        "aliases": [
            "O-SE",                                             # 略号
            "オーバーフレームシークレット",
            "オーバーフレームシークレットレア",
        ],
    },
    {
        "canonical": "OFウルトラ",
        "slug": "of-ultra",
        "order": 80,
        "color": "#ffd700",
        "aliases": [
            "O-UR",                                             # 略号（まんぞく屋/駿河屋）
            "オーバーフレームウルトラ",                         # フル表記
            "オーバーフレームウルトラレア",                     # 〜レア形式
        ],
    },
    {
        "canonical": "OFスーパー",
        "slug": "of-super",
        "order": 81,
        "color": "#4169e1",
        "aliases": [
            "O-SR",                                             # 略号
            "オーバーフレームスーパー",
            "オーバーフレームスーパーレア",
        ],
    },
    # ── 中レアリティ ──
    {
        "canonical": "コレクターズ",
        "slug": "collectors",
        "order": 85,
        "color": "#1abc9c",
        "aliases": [
            "CR",                                               # 略号
            "コレ",                                             # 短縮
            "コレクターズレア",                                 # 〜レア形式
            "コレレア",                                         # 短縮
        ],
    },
    {
        "canonical": "グランドマスター",
        "slug": "grandmaster",
        "order": 87,
        "color": "#00ced1",
        "aliases": [
            "GXR", "GMR",                                       # 略号
            "グランドマスターレア",                             # 〜レア形式
        ],
    },
    {
        "canonical": "アルティメット",
        "slug": "ultimate",
        "order": 90,
        "color": "#e67e22",
        "aliases": [
            "UL",                                               # 略号
            "アル", "レリ",                                     # 短縮（レリーフ）
            "レリーフ",                                         # 旧名
            "アルティメットレア",                               # 〜レア形式
        ],
    },
    {
        "canonical": "ホログラフィック",
        "slug": "holographic",
        "order": 95,
        "color": "#f39c12",
        "aliases": [
            "HR", "P-HR",                                       # 略号
            "ホロ",                                             # 短縮
            "ホログラフィックレア",                             # 〜レア形式
            "ホログラフィックパラレルレア",                     # パラレル版も同一視
            "ホログラフィックパラレル",
        ],
    },
    {
        "canonical": "ミレニアム",
        "slug": "millennium",
        "order": 100,
        "color": "#8e44ad",
        "aliases": [
            "M", "ML",                                          # 略号（Mは遊々亭、MLはカーナベル）
            "ミレ",                                             # 短縮
            "ミレニアムレア",                                   # 〜レア形式
        ],
    },
    {
        "canonical": "ウルトラ",
        "slug": "ultra",
        "order": 110,
        "color": "#e8c848",
        "aliases": [
            "UR",                                               # 略号
            "ウル",                                             # 短縮
            "ウルトラレア",                                     # 〜レア形式
        ],
    },
    {
        "canonical": "プレミアムゴールド",
        "slug": "premium-gold",
        "order": 115,
        "color": "#DAA520",
        "aliases": [
            "PG",                                               # 略号
            "プレゴル",                                         # 短縮
            "プレミアムゴールドレア",                           # 〜レア形式
        ],
    },
    {
        "canonical": "ゴールド",
        "slug": "gold",
        "order": 120,
        "color": "#DAA520",
        "aliases": [
            "GR", "GL",                                         # 略号（GRは遊々亭、GLはまんぞく屋/駿河屋）
            "ゴルレア",                                         # 短縮
            "ゴールドレア",                                     # 〜レア形式
        ],
    },
    {
        "canonical": "スーパーパラレル",
        "slug": "super-parallel",
        "order": 125,
        "color": "#5dade2",
        "aliases": [
            "P-SR",                                             # 略号（遊々亭）
            "スーパラ",                                         # 短縮
            "スーパーパラレルレア",                             # 〜レア形式
        ],
    },
    {
        "canonical": "スーパー",
        "slug": "super",
        "order": 130,
        "color": "#3498db",
        "aliases": [
            "SR",                                               # 略号
            "スー",                                             # 短縮
            "スーパーレア",                                     # 〜レア形式
        ],
    },
    {
        "canonical": "ウルトラパラレル",
        "slug": "ultra-parallel",
        "order": 135,
        "color": "#f4d03f",
        "aliases": [
            "UP", "P-UR",                                       # 略号
            "ウルパラ",                                         # 短縮
            "ウルトラパラレルレア",                             # 〜レア形式
        ],
    },
    # ── 低レアリティ ──
    {
        "canonical": "KC",
        "slug": "kc",
        "order": 140,
        "color": "#16a085",
        "aliases": [
            "KCレア",
        ],
    },
    {
        "canonical": "ラッシュレア",
        "slug": "rush-rare",
        "order": 145,
        "color": "#e91e63",
        "aliases": [
            "RR",
        ],
    },
    {
        "canonical": "オーバーラッシュ",
        "slug": "over-rush",
        "order": 147,
        "color": "#ff5722",
        "aliases": [
            "OR",
            "オーバーラッシュレア",
        ],
    },
    {
        "canonical": "レア",
        "slug": "rare",
        "order": 150,
        "color": "#cd7f32",
        "aliases": [
            "R",                                                # 略号
            "レアレア",                                         # 重複避け用（念のため）
        ],
    },
    {
        "canonical": "ノーマルパラレル",
        "slug": "normal-parallel",
        "order": 155,
        "color": "#7f8c8d",
        "aliases": [
            "NP", "P-N",                                        # 略号
            "ノーパラ", "Nパラ",                               # 短縮
            "ノーマルパラレルレア",                             # 〜レア形式
            "NPR",                                              # まんぞく屋略号
        ],
    },
    {
        "canonical": "ノーマル",
        "slug": "normal",
        "order": 160,
        "color": "#888888",
        "aliases": [
            "N",                                                # 略号
            "ノー",                                             # 短縮
            "ノーマルレア",                                     # トレコロ等の「〜レア」表記（ノーマルに統一）
            "Nレア",                                            # 略号版
        ],
    },
    # ── 孤児・特殊レアリティ（色/順序を付与して孤児解消） ──
    {
        "canonical": "パラレル",
        "slug": "parallel",
        "order": 170,
        "color": "#95a5a6",
        "aliases": [
            "PR",                                               # まんぞく屋略号
            "パラ",                                             # 短縮
            "パラレルレア",                                     # 〜レア形式（冗長 suffix）
        ],
    },
    {
        "canonical": "字レア",
        "slug": "letter-rare",
        "order": 180,
        "color": "#7d3c98",
        "aliases": [],                                          # 別名なし・生表記がそのまま正規名
    },
]


# ── 内部構築（起動時に1回だけ実行） ────────────────────────────────

_ALIAS_TO_CANON: dict[str, str] = {}  # alias（canonical自身含む） → canonical
_CANON_TO_ENTRY: dict[str, dict] = {} # canonical → entry dict

def _build_lookup() -> None:
    """RARITIES から逆引き辞書を構築し、重複別名を検出する。"""
    for entry in RARITIES:
        canon = entry["canonical"]
        _CANON_TO_ENTRY[canon] = entry
        # canonical 自身も alias 扱いで登録
        for key in [canon] + entry.get("aliases", []):
            if key in _ALIAS_TO_CANON and _ALIAS_TO_CANON[key] != canon:
                # 重複別名はプログラムのバグなので起動時に警告
                warnings.warn(
                    f"[rarity] alias '{key}' が '{_ALIAS_TO_CANON[key]}' と '{canon}' に重複登録されています。"
                    f"後勝ちで '{canon}' を採用します。",
                    stacklevel=2,
                )
            _ALIAS_TO_CANON[key] = canon

_build_lookup()


# ── 公開 API ─────────────────────────────────────────────────────

def normalize_rarity(raw: str, shop: str | None = None) -> str:  # noqa: ARG001
    """レアリティ文字列を正規名に変換する。

    未知の表記はそのまま返す（誤縮約しない）。
    未知表記が検出された場合は WARN ログを出力するので、
    定期的にログを確認して aliases への追加を検討すること。

    Args:
        raw:  生のレアリティ文字列（店舗HTMLから抽出したもの）
        shop: 将来の店舗別分岐用（現在は未使用・全店共通）

    Returns:
        正規名。未知の場合は raw をそのまま返す。
    """
    if not raw:
        return ""
    s = raw.strip()
    result = _ALIAS_TO_CANON.get(s)
    if result is not None:
        return result
    # 未知表記は生のまま返す（誤縮約させない）
    logger.warning(
        "[rarity] 未知のレアリティ表記: '%s' — rarity.py の aliases への追加を検討してください。",
        s,
    )
    return s


def color_of(canonical: str) -> str:
    """正規名から色(HEX)を返す。未知は '#7f8c8d'（グレー）。"""
    entry = _CANON_TO_ENTRY.get(canonical)
    return entry["color"] if entry else "#7f8c8d"


def order_of(canonical: str) -> int:
    """正規名から表示順を返す。未知は 9999（末尾）。"""
    entry = _CANON_TO_ENTRY.get(canonical)
    return entry["order"] if entry else 9999


def slug_of(canonical: str) -> str:
    """正規名から CSS スラグを返す。未知は 'unknown'。"""
    entry = _CANON_TO_ENTRY.get(canonical)
    return entry["slug"] if entry else "unknown"


def ordered_canonicals() -> list[str]:
    """全正規名を order 昇順で返す。"""
    return [e["canonical"] for e in sorted(RARITIES, key=lambda e: e["order"])]


def config_for_frontend() -> dict:
    """フロントエンドへ供給する設定辞書を返す。

    Returns:
        {canonical: {color, slug, order}, ...}
        フロントでは window.RARITY_CONFIG として参照する。
    """
    return {
        e["canonical"]: {
            "color": e["color"],
            "slug":  e["slug"],
            "order": e["order"],
        }
        for e in RARITIES
    }
