"""
build_text_check_dict.py — 効果テキスト誤字検証（unreleased_text_check.py）用の辞書を作る

お手本は Supabase の ygores_cards（日本語の公式効果テキスト）。実行時に毎回全件を読むのは重いので、
この Script で辞書ファイル data/text_check_dict.json.gz を作ってリポジトリにコミットする。

辞書の中身:
  ngrams       公式の効果文に現れる3文字連鎖の集合（「」の中身・数字・丸数字は伏せ字化したもの）
  card_names   全カード名
  quote_words  公式テキストの「」内に出てくる語
  phrase       「名詞＋助詞＋動詞」の組み合わせの出現数（カード単位）。言い回しの照合用（kind="unknown_phrase"）
  meta         作成日時・元の枚数・n など

使い方（書き込みはローカルのファイルだけ。Supabase は読むだけ）:
  python tools/build_text_check_dict.py [--out data/text_check_dict.json.gz]

認証: SUPABASE_URL / SUPABASE_KEY（未設定なら ~/.cardsouba/watcher.env から補完）
再生成のタイミング: ygores の同期（sync_ygores.py）で新カードが増えた後。数ヶ月に1回で足りる。
"""

import gzip
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import unreleased_text_check as tc  # noqa: E402

_PAGE = 500
_QUOTE_RE = re.compile(r"「([^「」]+)」")

# ygores_cards.raw は2形式ある（ygores_repository._summarize 参照）:
#   ライブAPI形式 raw.cardData.ja.{name,effectText,pendulumEffectText}
#   GitHubミラー形式 raw.{name,effectText,pendEffect}
# 重い faqData などを転送しないよう、必要な項目だけを PostgREST の JSON パスで取る。
_SELECT = (
    "konami_id, "
    "live_name:raw->cardData->ja->>name, live_eff:raw->cardData->ja->>effectText, "
    "live_pend:raw->cardData->ja->>pendulumEffectText, "
    "mir_name:raw->>name, mir_eff:raw->>effectText, mir_pend:raw->>pendEffect"
)


def _load_env_fallback() -> None:
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        return
    env_path = os.path.join(os.path.expanduser("~"), ".cardsouba", "watcher.env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


def fetch_corpus(sb) -> list[dict]:
    """ygores_cards から [{"konami_id", "name", "texts": [効果文, P効果文...]}] を返す。"""
    rows, offset = [], 0
    while True:
        batch = (sb.table("ygores_cards").select(_SELECT)
                 .order("konami_id").range(offset, offset + _PAGE - 1).execute().data or [])
        rows.extend(batch)
        if len(batch) < _PAGE:
            break
        offset += _PAGE
    return rows_to_corpus(rows)


def rows_to_corpus(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        name = r.get("live_name") or r.get("mir_name") or ""
        texts = [t for t in (r.get("live_eff"), r.get("live_pend"), r.get("mir_eff"), r.get("mir_pend")) if t]
        out.append({"konami_id": r.get("konami_id"), "name": name, "texts": texts})
    return out


def build_dict(corpus: list[dict]) -> dict:
    """コーパスから辞書（JSONにできる dict）を作る。純粋関数。"""
    ngrams: set = set()
    names: set = set()
    quote_words: set = set()
    phrase_sets: list = []
    used = 0
    for c in corpus:
        card_triples: set = set()
        if c.get("name"):
            names.add(c["name"])
        for t in c["texts"]:
            for m in _QUOTE_RE.finditer(t):
                w = m.group(1).strip()
                if w and len(w) <= 40:
                    quote_words.add(w)
            # 散文（フレーバー）・英文は効果文のお手本にならないので n-gram には入れない
            if not tc.is_effect_like(t):
                continue
            ngrams |= tc.ngram_set(tc.token_string(tc.tokenize(t, c.get("name", ""))))
            card_triples |= tc.phrase_triples(t, c.get("name", ""))
            used += 1
        phrase_sets.append(card_triples)
    phrase = tc.PhraseTable.from_triple_sets(phrase_sets)
    # 伏せ字が絡む連鎖も辞書に入る（「」の外の文脈を照合するため）。改行は含まれない
    return {
        "ngrams": "\n".join(sorted(ngrams)),
        "card_names": sorted(names),
        "quote_words": sorted(quote_words),
        "phrase": phrase.to_json(),
        "meta": {
            "version": tc.VERSION,
            "n": tc.NGRAM_N,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "cards": len(corpus),
            "effect_texts_used": used,
            "ngram_count": len(ngrams),
            "phrase_verbs": len(phrase.verbs),
            "phrase_triples": len(phrase.triples),
        },
    }


def write_dict(data: dict, path: str) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # mtime=0・ファイル名なしで毎回同じバイト列になるようにする（内容が同じなら git の差分が出ない）
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return os.path.getsize(path)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = tc.DICT_PATH
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
    _load_env_fallback()
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
        print("SUPABASE_URL / SUPABASE_KEY が未設定です")
        return 1
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    corpus = fetch_corpus(sb)
    if len(corpus) < 5000:
        # 取得が不完全なまま辞書を上書きして誤検知だらけにしないための確認
        print(f"コーパスが少なすぎます（{len(corpus)}枚）。辞書を書き出しません")
        return 1
    data = build_dict(corpus)
    size = write_dict(data, out)
    m = data["meta"]
    print(f"書き出し: {out} ({size:,} バイト)  カード{m['cards']}枚 / 効果文{m['effect_texts_used']}件 / "
          f"3-gram {m['ngram_count']:,} / カード名 {len(data['card_names']):,} / 「」内語 {len(data['quote_words']):,} / "
          f"動詞 {m['phrase_verbs']:,} / 語の組み合わせ {m['phrase_triples']:,}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
