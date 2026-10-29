"""adoption_parse.py — 店舗・CS の大会結果ポストから、入賞テーマ・参加人数・分布を抜き出す（純関数のみ）

collect_tournament_posts.py が X から集めた本文をここに通し、tournament_posts 表に保存する。
本文は表にそのまま残すので、抜き出し方を直したら PARSER_VERSION を上げて読み直せる。

抜き出すもの:
  - is_ocg: 遊戯王 OCG の結果か（店のアカウントは他TCG・ラッシュデュエルの結果も出すため）
  - participants: 参加人数（「参加者9人」「8名での開催」など。チーム戦のチーム数は数えない）
  - placements: [{"rank": 1, "theme_raw": "光と闇の儀式", "theme": "光と闇の儀式"}, ...]
  - distribution: [{"theme_raw": "...", "theme": "...", "count": 3 or None}, ...]（CS の「デッキ分布」）

テーマ名の正規化（theme）は表記の揺れ（全角・空白・絵文字・#・末尾の「デッキ」）を揃えるだけ。
「光と闇」と「光と闇の儀式」のような別名の統合は、集計側で別名表を持って行う（まだ無い）。
"""

import re
import unicodedata

PARSER_VERSION = 1

# 遊戯王 OCG の結果とみなす語。店の投稿は「遊戯王OCG」「ランキングデュエル」（公式の店舗大会）を書く
_OCG_WORDS = ("遊戯王", "OCG", "ランキングデュエル")
# 他TCG・別ルール・デジタル版。これを含むものは OCG の結果に数えない
_OTHER_WORDS = (
    "ラッシュデュエル", "ラッシュ・デュエル", "ラッシュCS", "遊戯王RD", "#RD", "RD大会",
    "レギュラートーナメント",  # ラッシュデュエルの店舗大会の名前
    "GENESYS", "ジェネシス", "マスターデュエル", "デュエルリンクス",
    "ポケモン", "ポケカ", "デュエマ", "デュエル・マスターズ", "デュエルマスターズ", "ワンピース",
    "ヴァンガード", "バトスピ", "MTG", "マジック:ザ", "ユニオンアリーナ", "ドラゴンボール",
    "シャドウバース", "ウィクロス", "ヴァイス", "ラブライブ", "ガンダム", "ロルカナ", "デジモン",
    "スマッシュバース", "ホロライブ", "ユニアリ", "フラグシップ",
)

# 昔の禁止制限で遊ぶ大会（#遊戯王1103・#遊戯王1209 など）と独自ルールの大会（グリモCS）。今の値段とは関係しない
_RETRO_FORMAT = re.compile(r"遊戯王\s?\d{4}(?!年)|グリモ")

_RANK_WORDS = {
    "優勝": 1, "1位": 1, "一位": 1,
    "準優勝": 2, "2位": 2, "二位": 2,
    "3位": 3, "三位": 3, "ベスト4": 3, "4位": 4, "四位": 4,
}
# 長い語から照合する（「準優勝」を「優勝」と読まないため）
_RANK_RE = re.compile("|".join(sorted((re.escape(w) for w in _RANK_WORDS), key=len, reverse=True)))
# からふだ形式の「⚔ 〃 名前 【テーマ】」（上の行と同じ順位）
_DITTO = "〃"

_BRACKET = r"[【「『]([^】」』\n]{1,40})[】」』]"
_BRACKET_RE = re.compile(_BRACKET)
# 括弧のあとに続くと「人の名前」とみなす語
_PERSON_AFTER = re.compile(r"^\s*(?:さん|様|さま|氏|選手|くん|ちゃん|殿)")

# 絵文字・記号（数字の囲み 1️⃣ の FE0F/20E3 も含む）
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿"
    "️‍⃣〰〽㊗㊙]"
)
_URL = re.compile(r"https?://\S+")


def _prep(text: str) -> str:
    """NFKC・URL と絵文字の除去（「1️⃣6️⃣名」を「16名」にする）。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = _URL.sub(" ", t)
    return _EMOJI.sub("", t)


def normalize_theme(raw: str) -> str:
    """表記の揺れだけを揃える（全角英数→半角・空白・絵文字・#・中黒・=・+ を除く・末尾の「デッキ」を除く）。"""
    s = _prep(raw)
    # 「レイズ・ムーン」と「レイズムーン」、「アルス=マグナ+エルフェンノーツ」と「アルスマグナエルフェンノーツ」を揃える
    s = re.sub(r"[\s#＃・=+]+", "", s)
    s = s.strip("・,、。:：~〜!！?？")
    s = re.sub(r"(?:デッキ|deck)$", "", s, flags=re.IGNORECASE)
    return s


def is_ocg(text: str, assume_ocg: bool = False) -> bool:
    """assume_ocg: CS 主催者のアカウントは「遊戯王」と書かないことが多い（Mogi杯など）ので、
    他TCGの語が無ければ OCG とみなす。"""
    t = _prep(text)
    if any(w in t for w in _OTHER_WORDS) or _RETRO_FORMAT.search(t):
        return False
    return assume_ocg or any(w in t for w in _OCG_WORDS)


_N = r"(\d{1,3})"
_PARTICIPANTS = (
    re.compile(r"参加(?:者|人数)数?\s*[〈<(\[【]?\s*[)〉>\]】]?\s*[:は]?\s*" + _N + r"\s*(?:名|人)"),
    re.compile(_N + r"\s*(?:名|人)\s*(?:様|の方々?|の皆(?:さま|様))?\s*(?:での|の|に|が|で)?\s*(?:ご)?(?:参加|参戦|開催)"),
    re.compile(r"参(?:加|戦)[^\n]{0,12}?" + _N + r"\s*(?:名|人)"),
    # からふだ形式「10/25 あざらCS/第5回個人杯 19名」
    re.compile(r"(?:CS|杯|大会|戦)[^\n]{0,30}?\s" + _N + r"\s*名\s*$", re.MULTILINE),
    # 人数だけの行「8名」
    re.compile(r"^\s*" + _N + r"\s*名\s*$", re.MULTILINE),
)


def parse_participants(text: str) -> int | None:
    t = _prep(text)
    for pat in _PARTICIPANTS:
        m = pat.search(t)
        if m:
            n = int(m.group(1))
            if 2 <= n <= 999:
                return n
    return None


def _clean_theme(raw: str | None) -> str | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or len(raw) > 40:
        return None
    # 「U-KYO【ブリッツクリーク】」は括弧の中をテーマとみなす
    inner = re.search(r"【([^】]{1,40})】", raw)
    if inner:
        raw = inner.group(1)
    theme = normalize_theme(raw)
    if not theme or theme.isdigit() or re.search(r"(さん|様|氏|選手)$", theme):
        return None
    return theme


def _theme_after(window: str) -> str | None:
    """順位の語のうしろの数行から、テーマ名を1つ拾う（「【デッキ】X」「使用デッキ：X」「「X」を使用」）。"""
    m = re.search(r"(?:【デッキ名?】|(?:使用デッキ|デッキ名?|(?<![A-Za-z])deck)(?=[\s:は【「『]))[ \t:は]*\n?\s*[【「『]?([^】」』\n、,]{1,40})",
                  window, re.IGNORECASE)
    if m:
        return m.group(1)
    # 「「ワユウ 様」#ダイソンスフィア」「「カイセイ. 様」#烙印+#星辰」（人名の括弧のあとのハッシュタグ）
    m = re.search(r"(?:様|さん)\s*[」』】]\s*(#[^\n]{1,40})", window)
    if m:
        return m.group(1)
    # 「寝業キング様の\n【レイズムーン】でした」
    m = re.search(r"(?:様|さん)の\s*\n?\s*" + _BRACKET, window)
    if m:
        return m.group(1)
    m = re.search(_BRACKET + r"\s*(?:を使用|使用|デッキ|で優勝)", window)
    if m:
        return m.group(1)
    return None


def _brackets_after(rest: str) -> list[str]:
    """「優勝は【A】【B】」「優勝 名前 【A】」の括弧。人名（後ろに さん 等）は除く。"""
    out = []
    pos = 0
    for b in _BRACKET_RE.finditer(rest):
        between = rest[pos:b.start()]
        if re.search(r"[。!?\n]", between) or len(between) > 14:
            break
        # 「優勝コメントは『楽しいデッキです』」「優勝者名【X】」
        if re.search(r"コメント|一言|名前|勝者名|者名", between):
            break
        pos = b.end()
        if _PERSON_AFTER.search(rest[b.end():]) or re.search(r"(?:さん|様|氏|選手)\s*$", b.group(1)):
            continue
        out.append(b.group(1))
    return out


def parse_placements(text: str) -> list[dict]:
    """順位とテーマの組を拾う。チーム戦（優勝は【A】【B】【C】）は同じ順位で複数入る。"""
    t = _prep(text)
    found: list[dict] = []
    seen: set[tuple[int, str]] = set()

    def add(rank: int, raw: str | None) -> bool:
        theme = _clean_theme(raw)
        if theme is None:
            return False
        if (rank, theme) not in seen:
            seen.add((rank, theme))
            found.append({"rank": rank, "theme_raw": raw.strip(), "theme": theme})
        return True

    lines = t.split("\n")
    last_rank = None
    for i, line in enumerate(lines):
        hits = list(_RANK_RE.finditer(line))
        if not hits and _DITTO in line and last_rank is not None:
            for raw in _brackets_after(line[line.index(_DITTO) + 1:]):
                add(last_rank, raw)
            continue
        for m in hits:
            rank = _RANK_WORDS[m.group(0)]
            last_rank = rank
            rest = line[m.end():]
            # 「一位:光と闇」「優勝：巳剣」
            m2 = re.match(r"\s*(?:は|者)?\s*:\s*([^\n、,()]{1,40})", rest)
            if m2 and not _PERSON_AFTER.search(rest[m2.end():]):
                val = m2.group(1)
                inner = _BRACKET_RE.fullmatch(val.strip())
                if add(rank, inner.group(1) if inner else val):
                    continue
            brs = _brackets_after(rest)
            if brs:
                for raw in brs:
                    add(rank, raw)
                continue
            # 「🏆優勝　光と闇（ピラふ）」「3位　恐竜（おじゃま）」（テーマのあとの括弧が人名）
            m3 = re.match(r"\s+([^\s(【「『:][^\n(]{0,30}?)\s*\([^)\n]{1,20}\)", rest)
            if m3 and add(rank, m3.group(1)):
                continue
            # 下の数行に「【デッキ】X」「「X」を使用」（次の順位の語の手前まで）
            window = "\n".join([rest] + lines[i + 1:i + 7])
            nxt = _RANK_RE.search(window, 1)
            if nxt:
                window = window[:nxt.start()]
            add(rank, _theme_after(window))
    return found


# 「デッキ分布:A、B」「分布\nA 3\nB 2」。見出しは行の頭か「デッキ」「使用」のあと
_DIST_HEAD = re.compile(r"(?:^|\n|デッキ|使用)\s*分布\s*(?:[:】\]]\s*|\n)")
_DIST_ITEM = re.compile(r"^(.+?)\s*(?:[×x✕*:]\s*(\d{1,3})|\(\s*(\d{1,3})\s*\)|\s(\d{1,3})\s*(?:名|人|件)?|(\d{1,3})\s*(?:名|人|件))?$")


def parse_distribution(text: str) -> list[dict]:
    t = _prep(text)
    m = _DIST_HEAD.search(t)
    if not m:
        return []
    tail = t[m.end():]
    first_line, _, rest = tail.partition("\n")
    items: list[str] = []
    if re.search(r"[、,/]", first_line):
        items = re.split(r"[、,/]", first_line)
    else:
        if first_line.strip():
            items.append(first_line)
        for line in rest.split("\n"):
            if not line.strip():
                if items:
                    break
                continue
            if len(line) > 30:
                break
            items.append(line)
    out: list[dict] = []
    seen: set[str] = set()
    for raw in items:
        raw = re.sub(r"^\s*(?:\d{1,2}\s*[:.)]|[・\-●○■□◆◇])\s*", "", raw).strip()
        if not raw or raw.startswith("#") or len(raw) > 30:
            continue
        mm = _DIST_ITEM.match(raw)
        name, count = raw, None
        if mm:
            name = mm.group(1)
            num = next((g for g in mm.groups()[1:] if g), None)
            count = int(num) if num else None
        inner = _BRACKET_RE.fullmatch(name.strip())
        if inner:
            name = inner.group(1)
        theme = _clean_theme(name)
        if theme is None or theme in seen:
            continue
        seen.add(theme)
        out.append({"theme_raw": name.strip(), "theme": theme, "count": count})
    return out


def parse_post(text: str, assume_ocg: bool = False) -> dict:
    """1件のポストを読む。保存する列の形で返す。"""
    ocg = is_ocg(text, assume_ocg)
    return {
        "is_ocg": ocg,
        "participants": parse_participants(text) if ocg else None,
        "placements": parse_placements(text) if ocg else [],
        "distribution": parse_distribution(text) if ocg else [],
        "parser_version": PARSER_VERSION,
    }
