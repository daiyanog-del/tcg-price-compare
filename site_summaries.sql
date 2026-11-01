-- =============================================
-- 重い集計の作り置き（site_summaries。2026-11-01）
-- =============================================
-- 背景: 相場の最安・値動きランキング3種・汎用カードの値動きは、空いていれば0.02〜0.38秒の集計だが、Supabase 無料 Nano の
--   ディスクの余力が小さく、毎朝の収集（5〜10時 JST）・新弾の収集・デプロイ直後の一斉読み込みと重なると8秒の打ち切り
--   （statement timeout 57014）に当たっていた（1週間で約55件）。打ち切られるまでの間はほかの軽い読み込みも巻き添えで遅れる。
--   元のデータは収集のときにしか大きく変わらないのに、サーバーごとに5分〜1時間おきに同じ計算を繰り返していた。
-- 方針（ユーザー選択「A: DBで作り置き」）:
--   - 頼む: Web が「この RPC をこの引数で」を kind ごとに1行書く（kind・rpc・params）。引数の正本は Python の定数のまま
--     （休止中の店・レアリティの組・日付の区切りを DB に写さない）
--   - 作る: pg_cron が10分ごとに refresh_site_summaries() を CALL し、各行の rpc を params のとおりに呼んで payload に置く
--     （params_used に実際に使った引数・computed_epoch に作った時刻）。管理ユーザーで動くので Web の8秒の打ち切りに当たらない。
--     1行ごとに確定する（全部終わるまで行を握ると、Web の依頼の書き込みが待たされて打ち切られる。reviewer 中3）。
--     10分ごとでも、サーバーごとに5分〜1時間おきに計算していた今までより回数はずっと少なく、新しさは相場の今までの10分と同じ
--   - 読む: Web（site_summary.fetch）は params_used が今渡す引数と完全に同じで、作ってから一定時間内の payload だけを使う。
--     違う・古い・無いときは今までどおりその場で RPC を呼ぶ（定期実行が止まってもサイトは止まらない）
-- 書き手: site_summary.register（Web・kind/rpc/params だけ）と refresh_site_summaries（pg_cron・結果の列だけ）
-- 読み手: site_summary.fetch（app.py の相場キャッシュ・値動きランキング3種、staple_ranking）
-- 適用順: このファイルを先に適用 → コードをデプロイ（コードが先でも、表が無ければ毎回その場で計算するだけで動く）
-- 戻し方: コードを戻す → SELECT cron.unschedule('refresh-site-summaries') → DROP PROCEDURE refresh_site_summaries
--   → DROP FUNCTION refresh_site_summary, site_summary_call → DROP TABLE site_summaries（pg_cron 自体は残してよい）

CREATE TABLE IF NOT EXISTS public.site_summaries (
    kind           text PRIMARY KEY,           -- 'best_prices'・'top_movers:all' など（site_summary.py の呼び出し側が決める）
    rpc            text NOT NULL CHECK (rpc IN ('get_card_best_prices_json', 'get_top_movers', 'get_price_movers',
                                               'get_buyback_movers', 'get_staple_movers')),
    params         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- Web が渡す引数（名前→値。配列は文字列の配列）
    params_used    jsonb,                      -- payload を作ったときの引数（Web はこれが今の引数と同じときだけ使う）
    payload        jsonb,                      -- RPC の結果（行の配列。get_card_best_prices_json はその値そのもの）
    computed_epoch double precision,           -- payload を作った時刻（UNIX 秒。Web が古さを測る）
    error          text,                       -- 最後の作り直しの失敗（成功したら空に戻す）
    updated_at     timestamptz NOT NULL DEFAULT now()
);
-- Web は service_role（RLS を通らない）で読み書きする。anon・authenticated には見せない（RLS に加えて表の権限も外す。reviewer 低9）
ALTER TABLE public.site_summaries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.site_summaries FROM anon, authenticated;

-- params のとおりに rpc を呼んだ結果を返す（名前つき引数。params に無い引数は関数の既定値）
CREATE OR REPLACE FUNCTION public.site_summary_call(p_rpc text, p_params jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_args text;
    v_result jsonb;
BEGIN
    IF p_rpc NOT IN ('get_card_best_prices_json', 'get_top_movers', 'get_price_movers',
                     'get_buyback_movers', 'get_staple_movers') THEN
        RAISE EXCEPTION 'site_summary_call: 扱わない rpc %', p_rpc;
    END IF;
    SELECT string_agg(format('%I => %L', key,
               CASE jsonb_typeof(value)
                   WHEN 'array' THEN (SELECT coalesce(array_agg(e), '{}')::text FROM jsonb_array_elements_text(value) e)
                   ELSE value #>> '{}'
               END), ', ')
      INTO v_args
      FROM jsonb_each(coalesce(p_params, '{}'::jsonb));
    IF p_rpc = 'get_card_best_prices_json' THEN
        EXECUTE format('SELECT public.%I(%s)', p_rpc, coalesce(v_args, '')) INTO v_result;
    ELSE
        EXECUTE format('SELECT coalesce(jsonb_agg(to_jsonb(t)), ''[]''::jsonb) FROM public.%I(%s) t',
                       p_rpc, coalesce(v_args, '')) INTO v_result;
    END IF;
    RETURN v_result;
END;
$$;

-- 1行を作り直す。失敗しても例外を外へ出さず error に残す（他の行を止めない）。作り直せたら true
CREATE OR REPLACE FUNCTION public.refresh_site_summary(p_kind text)
RETURNS boolean
LANGUAGE plpgsql
SET search_path = public, pg_temp
SET work_mem = '64MB'
AS $$
DECLARE
    j record;
    v_result jsonb;
BEGIN
    SELECT kind, rpc, params INTO j FROM public.site_summaries WHERE kind = p_kind;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    BEGIN
        -- 先に計算してから短く書く（行を握るのは UPDATE の間だけ）
        v_result := public.site_summary_call(j.rpc, j.params);
        UPDATE public.site_summaries
           SET payload = v_result, params_used = j.params,
               computed_epoch = extract(epoch FROM clock_timestamp()), error = NULL, updated_at = now()
         WHERE kind = j.kind AND params = j.params;  -- 計算中に依頼の引数が変わっていたら書かない（次の回に作り直す）
        RETURN FOUND;
    EXCEPTION WHEN OTHERS THEN
        UPDATE public.site_summaries SET error = left(SQLERRM, 500), updated_at = now() WHERE kind = j.kind;
        RETURN false;
    END;
END;
$$;

-- 頼まれた行をすべて、1行ずつ確定しながら作り直す（pg_cron から CALL。トランザクションの外で呼ぶこと）
-- SET を付けた手続きは COMMIT できないので search_path は付けない（名前はすべて public. つき）
CREATE OR REPLACE PROCEDURE public.refresh_site_summaries()
LANGUAGE plpgsql
AS $$
DECLARE
    k text;
BEGIN
    FOR k IN SELECT kind FROM public.site_summaries ORDER BY kind LOOP
        PERFORM public.refresh_site_summary(k);
        COMMIT;
    END LOOP;
END;
$$;

-- どれも Web（PostgREST）からは呼ばせない（pg_cron は postgres で動く）
REVOKE ALL ON FUNCTION public.site_summary_call(text, jsonb) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.refresh_site_summary(text) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON PROCEDURE public.refresh_site_summaries() FROM PUBLIC, anon, authenticated, service_role;

-- 10分ごと（3分・13分…。ちょうどの時刻に始まる他の定期実行と重ねない）
CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA pg_catalog;
SELECT cron.schedule('refresh-site-summaries', '3-59/10 * * * *', 'CALL public.refresh_site_summaries()');
