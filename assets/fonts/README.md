# 画像用の書体

海外先行の予告カードの OG 画像（tcg_announce_og.py）で使う。X の値動き投稿の画像（staple_post.py）と同じ書体。

- MPLUS2.ttf … M PLUS 2（可変・wght）。和文。https://github.com/google/fonts/tree/main/ofl/mplus2 （SIL Open Font License 1.1）
- Outfit.ttf … Outfit（可変・wght）。英字のカード名・数字。https://github.com/google/fonts/tree/main/ofl/outfit （SIL Open Font License 1.1）

M PLUS 2 に無い字（★・※・ギリシャ文字・丸数字など）は、本番の Docker に入っている Noto Sans CJK（deck_image._get_font）で補う。
