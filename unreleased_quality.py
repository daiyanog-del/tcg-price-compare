"""未発売カードの構造化応答・拡大再読・公式発売日の照合。"""
import base64
import copy
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# 抽出AIが「不確かだった欄」として返せる値（構造化出力の enum）
UNCERTAIN_FIELDS = (
    "name", "effect_text", "pendulum_effect", "card_type", "attribute", "race",
    "level_rank_link", "atk", "def", "pendulum_scale", "product_name", "release_date",
)

# 不確かだと自動承認しない欄。データが溜まるまでは安全側に倒し、attribute / race も含める。
# 外すのは次の2つだけ:
#   release_date: 取込時に公式の発売日表と別途照合する（release_date_not_verified）ので自己申告に頼らない
#   product_name: その照合に使われ、不一致は発売日側で落ちる
# 緩める判断は tools/auto_approve_report.py の「不確か欄ごとの手直し率」を見てから行う。
IMPORTANT_UNCERTAIN_FIELDS = frozenset({
    "name", "effect_text", "pendulum_effect", "card_type", "attribute", "race",
    "level_rank_link", "atk", "def", "pendulum_scale",
})

UNCERTAIN_FIELD_LABELS = {
    "name": "カード名", "effect_text": "効果テキスト", "pendulum_effect": "ペンデュラム効果",
    "card_type": "カード種別", "attribute": "属性", "race": "種族",
    "level_rank_link": "レベル/ランク/リンク", "atk": "攻撃力", "def": "守備力",
    "pendulum_scale": "ペンデュラムスケール", "product_name": "収録パック", "release_date": "発売日",
}


def strict_schema(schema):
    """Pydanticのスキーマを全項目必須のAPI構造化出力へ変換する。"""
    schema = copy.deepcopy(schema)
    def visit(value):
        if isinstance(value, dict):
            value.pop('default', None)
            value.pop('title', None)
            if value.get('type') == 'object':
                value['additionalProperties'] = False
                value['required'] = list(value.get('properties', {}))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(schema)
    return schema


def structured_request(client, *, model, system, content, schema, max_tokens=16000, effort=None):
    """不完全な応答は成功0件に変換せず失敗として呼出側に伝える。

    effort（"low"/"medium"/"high"）を渡すと output_config.effort に入れる。None なら送らない（モデル既定）。
    thinking・temperature などサンプリング指定は送らない（Opus 5.5 は thinking を無効化できない）。
    """
    output_config = {'format': {'type': 'json_schema', 'schema': strict_schema(schema)}}
    if effort:
        output_config['effort'] = effort
    message = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{'role': 'user', 'content': content}],
        extra_body={'output_config': output_config},
    )
    if message.stop_reason != 'end_turn':
        raise ValueError(f'抽出応答が未完了です: {message.stop_reason}')
    text = ''.join(block.text for block in message.content if getattr(block, 'type', '') == 'text')
    data = json.loads(text)
    return data, text, {'input_tokens': message.usage.input_tokens, 'output_tokens': message.usage.output_tokens}


def lacks_card_body(row: dict) -> bool:
    """抽出結果が「カード本体（枠・文字）を読めていない」か判定する。

    イラストのみ・ティザー画像の投稿は、カード名しか分からず次をすべて満たす:
      - effect_text と pendulum_effect が空（効果・フレーバーとも読めていない）
      - atk / level / rank / link_val がすべて None（数値欄が読めていない）
    通常モンスターは効果がなくてもフレーバーテキストと ATK/DEF・レベルが印刷されており、
    魔法・罠は効果文が必ずあるため、本体が写っていれば上記のどれかは埋まる。
    1つでも埋まっていれば「本体あり」とみなし、誤って保留しない（保留は後続投稿を待つだけで
    データは失わないが、誤判定を減らして従来動作を保つ）。
    実例: 2026-10-14「竜魔導の降霊師」21:30 イラストのみ投稿が先に挿入され、
    21:00 の本体画像投稿が取込済みとしてスキップされた。
    """
    if (row.get("effect_text") or "").strip():
        return False
    if (row.get("pendulum_effect") or "").strip():
        return False
    return all(row.get(k) is None for k in ("atk", "level", "rank", "link_val"))


# 記事内の日付を補助採用する上限（今日から何日先まで）。AIの年誤り（過去日）対策の範囲。
# TODO: calibrate from data
_ARTICLE_DATE_MAX_DAYS = 400


def _article_date_adoptable(value):
    """記事から読んだ日付が今日(JST)以上かつ上限日数以内の有効なISO日付か。"""
    from constants import JST
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return False
    today = datetime.now(JST).date()
    return today <= parsed <= today + timedelta(days=_ARTICLE_DATE_MAX_DAYS)


_REVIEW_SCHEMA = {
    'type': 'object', 'properties': {'cards': {'type': 'array', 'items': {
        'type': 'object', 'properties': {
            'id': {'type': 'integer'},
            'level': {'type': ['integer', 'null']},
            'rank': {'type': ['integer', 'null']},
            'link_val': {'type': ['integer', 'null']},
            'readable': {'type': 'boolean'},
        },
    }}},
}


def enrich_rows(rows, *, client, model, encoded_images):
    """初回候補を元画像の境界で補正し、上部拡大で数値を、効果欄の拡大で効果テキストを読み直す。"""
    from card_crop import refine_card_box, make_level_zoom
    from release_date_resolver import resolve_release_date
    images = {image['url']: image for image in encoded_images}
    pending = []
    dates = {}
    for index, row in enumerate(rows):
        raw = row['extraction_raw']
        product = row.get('product_name') or ''
        if product not in dates:
            dates[product] = resolve_release_date(product)
        evidence = copy.deepcopy(dates[product])
        evidence['product_name'] = product
        evidence['candidate_date'] = row.get('release_date')
        raw['release_date_evidence'] = evidence
        row['release_date'] = evidence['release_date'] if evidence['status'] == 'verified' else None
        if evidence['status'] != 'verified':
            # 公式で確認できない場合のみ、記事内の日付を範囲内なら補助採用（conflictは採用しない）。
            if evidence['status'] != 'conflict' and _article_date_adoptable(evidence['candidate_date']):
                row['release_date'] = evidence['candidate_date']
                evidence['adopted_from'] = 'article'
            logger.warning('発売日要確認: %s: %s', row['name'], evidence['reason'])
        raw['recognition_review'] = {'status': 'needs_review', 'reason': '拡大再読の対象画像・枠が確認できません'}
        image = images.get(raw.get('source_image_url'))
        bbox = raw.get('card_bbox')
        if not image or not bbox:
            row['confidence'] = 'medium' if row['confidence'] == 'high' else row['confidence']
            continue
        content = base64.b64decode(image['data'], validate=True)
        suggestion = refine_card_box(content, bbox)
        if not suggestion:
            logger.warning('クロップ候補を検証できません: %s', row['name'])
            row['confidence'] = 'medium' if row['confidence'] == 'high' else row['confidence']
            continue
        suggestion['source_image_url'] = image['url']
        raw['crop_suggestion'] = suggestion
        zoom = make_level_zoom(content, suggestion)
        if not zoom:
            row['confidence'] = 'medium' if row['confidence'] == 'high' else row['confidence']
            continue
        pending.append((index, row, image, zoom))

    # 1対象2画像。APIの20画像超過時制限を避け、小画像の取り違えを抑える。
    # TODO: calibrate from data - 12画像/要求の予備実験を基準に6カードへ制限。
    for start in range(0, len(pending), 6):
        batch = pending[start:start + 6]
        content = []
        for index, row, image, zoom in batch:
            content.extend([
                {'type': 'text', 'text': f'ID={index} 対象カード名={row["name"]}。次の元画像と上部拡大のみで数値を確認してください。'},
                {'type': 'image', 'source': {'type': 'base64', 'media_type': image['media_type'], 'data': image['data']}},
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': base64.b64encode(zoom).decode()}},
            ])
        data, text, usage = structured_request(
            client, model=model, content=content, schema=_REVIEW_SCHEMA, max_tokens=6000,
            system='遊戯王OCGカードの星を1個ずつ数え、レベル・ランク・リンク値を確認してください。エクシーズの黒い星はrank、通常の星はlevel、リンクはlink_val。魔法罠は全てnull。対象名のカードだけを読み、未知や判読不能を記憶で補わない。判読不能ならreadable=false、数値null。判読できればreadable=true。各IDを一度ずつ返す。',
        )
        answers = data.get('cards', [])
        expected = {item[0] for item in batch}
        ids = [a.get('id') for a in answers]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise ValueError('拡大再読のカードIDに欠落または重複があります')
        by_id = {a['id']: a for a in answers}
        for index, row, image, zoom in batch:
            answer = by_id[index]
            values = [answer.get(k) for k in ('level', 'rank', 'link_val')]
            valid = all(v is None or (type(v) is int and v >= 0) for v in values)
            valid = valid and sum(v is not None for v in values) <= 1
            monster = 'モンスター' in row.get('card_type', '')
            valid = valid and (any(v is not None for v in values) if monster else all(v is None for v in values))
            if monster:
                kind = row['card_type']
                required = 'rank' if 'エクシーズ' in kind else 'link_val' if 'リンク' in kind else 'level'
                valid = valid and answer.get(required) is not None
            valid = valid and answer.get('readable') is True
            raw = row['extraction_raw']
            raw['review_usage'] = usage
            raw['review_raw_response'] = text
            raw['recognition_review'] = {'status': 'verified' if valid else 'needs_review', 'reason': '上部拡大で再読しました' if valid else '上部拡大でも数値・カード種別を一意に確認できません'}
            for key in ('level', 'rank', 'link_val'):
                row[key] = answer.get(key) if valid else None
            raw['recognition_review']['values'] = {key: row[key] for key in ('level', 'rank', 'link_val')}
            raw['recognition_review']['card_type'] = row.get('card_type', '')
            if not valid:
                row['confidence'] = 'low'
                logger.warning('数値要確認: %s', row['name'])
    # 効果テキストの拡大読み直しと1回目との突き合わせ（全カード。X・公式・管理画面の取込すべてがここを通る）
    from effect_text_reread import reread_effect_texts
    reread_effect_texts(rows, client=client, encoded_images=encoded_images)
    return rows
