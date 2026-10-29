"""adoption_parse の抜き出しのテスト。例文は 2026-10-29 に集めた店・CS の投稿の書き方を写したもの（名前は架空）。"""

from adoption_parse import (
    is_ocg,
    normalize_theme,
    parse_distribution,
    parse_participants,
    parse_placements,
    parse_post,
)


def themes(text):
    return [(p["rank"], p["theme"]) for p in parse_placements(text)]


# ── OCG かどうか ───────────────────────────────

def test_is_ocg_shop_and_other_tcg():
    assert is_ocg("【#遊戯王OCG イベント結果】優勝は…")
    assert is_ocg("本日のランキングデュエルの優勝者は")
    assert not is_ocg("#遊戯王RD レギュラートーナメント 優勝者は")
    assert not is_ocg("🔥ポケモンカード🔥 優勝 遊戯王も開催中")
    assert not is_ocg("#遊戯王マスターデュエル 分布")
    # 昔の禁止制限の大会・独自ルールの大会
    assert not is_ocg("#寒波亭 #遊戯王1103 デッキ分布")
    assert not is_ocg("#グリモCS 遊戯王 大会結果")
    # 年の「遊戯王2026年」は昔のルールではない
    assert is_ocg("遊戯王2026年10月 CS結果")


def test_is_ocg_assume_for_cs_organizer():
    # CS 主催者は「遊戯王」と書かない（Mogi杯など）
    assert not is_ocg("第26回Mogi杯 デッキ分布")
    assert is_ocg("第26回Mogi杯 デッキ分布", assume_ocg=True)
    assert not is_ocg("ラッシュCS 結果", assume_ocg=True)


# ── 参加人数 ──────────────────────────────────

def test_participants_variants():
    assert parse_participants("〈参加者〉9人") == 9
    assert parse_participants("(1デュエル戦・スイスドロー・参加者3名)") == 3
    assert parse_participants("（参加人数8️⃣名、戦績3-0）") == 8
    assert parse_participants("1️⃣6️⃣名での開催となりました") == 16
    assert parse_participants("本日は11名様で開催しました") == 11
    assert parse_participants("1⃣3⃣名の方々にご参加いただきました") == 13
    assert parse_participants("14名様のご参加でした") == 14
    assert parse_participants("参戦デュエリストは6名でした") == 6
    assert parse_participants("99名の皆さまにご参加いただいております") == 99
    assert parse_participants("⚔　10/25　あざらCS/第5回個人杯　19名\n⚔ #遊戯王") == 19
    assert parse_participants("優勝おめでとうございます") is None


# ── 入賞テーマ ─────────────────────────────────

def test_placement_bracket_with_use():
    text = "本日の優勝を勝ち取られたのは⋯\n「ブリッツクリーク」を使用された、\n『テスト中村』さんでした"
    assert themes(text) == [(1, "ブリッツクリーク")]


def test_placement_person_bracket_then_deck_line():
    text = "優勝は【傘】さんです✨\n\n使用デッキ【三幻魔】\nコメント【よろしく】"
    assert themes(text) == [(1, "三幻魔")]


def test_placement_deck_colon_and_latin_deck():
    assert themes("優勝者は…\n🏆テスト様🏆\n使用デッキ：M∀LICE\nコメント：一言") == [(1, "M∀LICE")]
    assert themes("優勝者をご紹介‼️\n\nName : テスト 様\ndeck : 三幻魔\ncomment：強かった") == [(1, "三幻魔")]
    assert themes("優勝　　　🏅テストさん🏅\nデッキ名　アルスマグナエルフェンノーツ\nコメント　一言") == \
        [(1, "アルスマグナエルフェンノーツ")]


def test_placement_name_and_deck_labels():
    text = "🏆優勝🏆\n【名前】テストさん\n【デッキ】ニビル\n【一言】学校CSをよろしく！"
    assert themes(text) == [(1, "ニビル")]


def test_placement_deck_on_far_line():
    text = ("本日の優勝者は…❓\n【テスト】選手です🏆\nおめでとうございます‼️\n\n"
            "デッキ \n『光闇ホルスブラマジ』\nなっが～い")
    assert themes(text) == [(1, "光闇ホルスブラマジ")]


def test_placement_hashtag_after_person():
    assert themes("🏆優勝🏆\n「テスト. 様」#烙印＋#星辰") == [(1, "烙印星辰")]
    assert themes("🏆優勝🏆 「テスト 様」#妖精伝姫ウィッチクラフト") == [(1, "妖精伝姫ウィッチクラフト")]


def test_placement_name_only_is_empty():
    assert themes("優勝者は【テスト】様です👑") == []
    assert themes("優勝は「テスト」さんでした❗") == []


def test_placement_team_brackets():
    text = "【遊戯王大会結果】第3回テストCS チーム戦　優勝は【烙印光と闇の儀式】【アルス＝マグナエルフェンノーツ】【三幻魔】！"
    assert themes(text) == [(1, "烙印光と闇の儀式"), (1, "アルスマグナエルフェンノーツ"), (1, "三幻魔")]


def test_placement_ranked_lines():
    text = "10/25開催第16回テストCS結果\n\n一位:光と闇\n二位:岩石GS\n三位:巳剣光と闇\n"
    assert themes(text) == [(1, "光と闇"), (2, "岩石GS"), (3, "巳剣光と闇")]


def test_placement_karafuda_format_with_ditto():
    text = ("◤#からふだ◢\n⚔　優勝　あいう　【道化の一座】\n⚔　2位　かきく　【三幻魔】\n"
            "⚔　ベスト4　さしす　【M∀LICE】\n⚔　〃　たちつ　【D-HERO】\n⚔\n"
            "⚔　10/25　テストCS　18名\n⚔　#遊戯王 #遊戯王OCG #デッキレシピ #大会結果")
    assert themes(text) == [(1, "道化の一座"), (2, "三幻魔"), (3, "M∀LICE"), (3, "D-HERO")]


def test_placement_not_confused_by_hashtag_recipe():
    # 「#デッキレシピ」をデッキ名と読まない
    text = "⚔　4位　あいう\n⚔　#遊戯王 #デッキレシピ #大会結果"
    assert themes(text) == []


def test_runner_up_not_read_as_winner():
    assert themes("準優勝：烙印") == [(2, "烙印")]


# ── 分布 ──────────────────────────────────────

def test_distribution_inline_list():
    d = parse_distribution("デッキ分布:巳剣光と闇、レイズムーン、アンブラル\n\n来てくれた方々ありがとう")
    assert [(x["theme"], x["count"]) for x in d] == [("巳剣光と闇", None), ("レイズムーン", None), ("アンブラル", None)]


def test_distribution_lines_with_counts():
    d = parse_distribution("【デッキ分布】\n三幻魔 5\nエルフェンノーツ×3\n光と闇(2)\n\n詳しくは画像")
    assert [(x["theme"], x["count"]) for x in d] == [("三幻魔", 5), ("エルフェンノーツ", 3), ("光と闇", 2)]


def test_distribution_not_from_prose():
    assert parse_distribution("参加デッキ分布や決勝トーナメントの結果をもとに振り返ります") == []
    assert parse_distribution("大会分布、結果を元に作成。") == []


# ── まとめ ────────────────────────────────────

def test_normalize_theme():
    assert normalize_theme("＃三幻魔 デッキ") == "三幻魔"
    assert normalize_theme("ワンダースター") == "ワンダースター"   # 末尾の長音を消さない
    assert normalize_theme("アルス＝マグナ＋エルフェンノーツ") == "アルスマグナエルフェンノーツ"
    assert normalize_theme("レイズ・ムーン") == normalize_theme("レイズムーン")


def test_parse_post_other_tcg_is_empty():
    r = parse_post("🔥ポケモンカード🔥\n〈参加者〉8人\n🏆優勝🏆\n【デッキ】ボムフーディン")
    assert r["is_ocg"] is False
    assert r["placements"] == [] and r["participants"] is None


# ── 2026-10-30 の試し実行で見つかった書き方 ─────────────

def test_placement_theme_then_person_in_parens():
    text = "10/28　夜の遊戯王公認大会\n🏆優勝　光と闇（あいう）\n　準優勝　レイズムーン（かきく）\n　3位　恐竜（さしす）"
    assert themes(text) == [(1, "光と闇"), (2, "レイズムーン"), (3, "恐竜")]


def test_placement_deck_wa_bracket():
    text = "優勝者は【テスト】様です！\n使用デッキは【ブラマジ黄金櫃】\nおめでとうございます"
    assert themes(text) == [(1, "ブラマジ黄金櫃")]


def test_placement_person_no_then_bracket():
    text = "本日開催\n#遊戯王\n8名\n\n優勝は\nテスト様の\n【レイズムーン】でした！"
    assert themes(text) == [(1, "レイズムーン")]
    assert parse_participants(text) == 8


def test_placement_comment_bracket_is_not_theme():
    text = "🏆優勝は 「テスト」様の\n『光と闇の原石青眼』デッキでした！！\n\n優勝コメントは『楽しいデッキです！』"
    assert themes(text) == [(1, "光と闇の原石青眼")]
