"""独自ドメインへの引き継ぎの結合確認（Playwright・手で実行する）。

設計文書: docs/design-domain-migration-2026-10-21.md §7
localhost:5099 を旧URL、127.0.0.1:5099 を新ドメインに見立てる（origin が分かれる）。

  起動（tcg-web で）:
    FLASK_DEBUG=0 PORT=5099 CANONICAL_BASE_URL=http://127.0.0.1:5099     LEGACY_HOSTS=localhost:5099 DISABLE_STARTUP_JOBS=1 python app.py
  実行:
    python tools/check_domain_handoff.py
"""
import json, sys, time
from playwright.sync_api import sync_playwright

OLD = "http://localhost:5099"
NEW = "http://127.0.0.1:5099"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("OK  " if cond else "NG  ") + name + (f"  {detail}" if detail and not cond else ""))


def deck(i, name, updated):
    return {"id": f"d_{i}", "name": name, "text": "3 灰流うらら", "main": [{"qty": 3, "name": "灰流うらら"}], "ex": [], "updated": updated}


def seed(page, origin, data):
    page.goto(origin + "/robots.txt")
    page.evaluate("d => { for (const [k,v] of Object.entries(d)) localStorage.setItem(k, v); }", data)


def dump(page, origin):
    page.goto(origin + "/robots.txt")
    return page.evaluate("() => Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))")


def wait_new(page, timeout=15000):
    page.wait_for_url(lambda u: u.startswith(NEW) and "/handoff" not in u, timeout=timeout)


with sync_playwright() as p:
    browser = p.chromium.launch()
    errors = []

    # ── 1. 初回: 丸ごと写る ──
    ctx = browser.new_context()
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    now = int(time.time() * 1000)
    seed(page, OLD, {
        "cardprice_saved_decks": json.dumps([deck(1, "A", now - 1000), deck(2, "B", now - 1000)]),
        "cardprice_wishlist": json.dumps([{"name": "灰流うらら", "qty": 1, "rarity": ""}]),
        "cardprice_sync_state": json.dumps({"sync_id": "11111111-1111-1111-1111-111111111111", "wishlist_rev": 3, "decks_rev": 4}),
        "sol-saved-replays": json.dumps([{"id": "r1"}]),
        "cardprice_deck_draft": json.dumps({"name": "draft"}),
        "admin_key": "secret",
    })
    hops = []
    page.on("framenavigated", lambda f: hops.append(f.url) if f == page.main_frame else None)
    page.goto(OLD + "/new-cards?x=1")
    wait_new(page)
    check("1 最後は新ドメインの同じパス", page.url == NEW + "/new-cards?x=1", page.url)
    check("1 /handoff を経由", any("/handoff" in h for h in hops), hops)
    check("1 # が履歴に残らない", all("#" not in h or "/handoff" not in h or h.endswith("/handoff?next=%2Fnew-cards%3Fx%3D1") for h in hops[-1:]), hops)
    new = dump(page, NEW)
    check("1 デッキが写る", json.loads(new.get("cardprice_saved_decks", "[]")) and len(json.loads(new["cardprice_saved_decks"])) == 2)
    check("1 購入候補が写る", len(json.loads(new.get("cardprice_wishlist", "[]"))) == 1)
    check("1 同期の状態が写る", "11111111" in new.get("cardprice_sync_state", ""))
    check("1 リプレイ・下書きが写る", "r1" in new.get("sol-saved-replays", "") and "draft" in new.get("cardprice_deck_draft", ""))
    check("1 管理画面の鍵は運ばない", "admin_key" not in new)
    old = dump(page, OLD)
    check("1 旧側のデータは残る", "cardprice_saved_decks" in old)
    check("1 新側に受け取りの印", "gl_handoff_received_at" in new)

    # ── 2. 2回目・変更なし: 新側のデータは変わらない ──
    page.goto(NEW + "/robots.txt")
    before = page.evaluate("() => localStorage.getItem('cardprice_saved_decks')")
    page.goto(OLD + "/")
    wait_new(page)
    page.wait_for_load_state("load")
    page.wait_for_timeout(800)
    check("2 新ドメインへ移る", page.url == NEW + "/", page.url)
    check("2 新側のデータは変わらない", page.evaluate("() => localStorage.getItem('cardprice_saved_decks')") == before)

    # ── 3. 新ドメインで消したデッキは復活しない・旧で後から増えたデッキだけ届く ──
    page.goto(NEW + "/robots.txt")
    page.evaluate("() => { const l = JSON.parse(localStorage.getItem('cardprice_saved_decks')).filter(d => d.id !== 'd_2'); localStorage.setItem('cardprice_saved_decks', JSON.stringify(l)); }")
    page.goto(OLD + "/robots.txt")
    page.evaluate("d => { const l = JSON.parse(localStorage.getItem('cardprice_saved_decks')); l.push(d); localStorage.setItem('cardprice_saved_decks', JSON.stringify(l)); }",
                  deck(3, "C(拡張で取り込み)", int(time.time() * 1000) + 5000))
    page.goto(OLD + "/")
    wait_new(page)
    page.wait_for_load_state("load")
    page.wait_for_timeout(800)
    ids = [d["id"] for d in page.evaluate("() => JSON.parse(localStorage.getItem('cardprice_saved_decks'))")]
    check("3 増えたデッキが届く", "d_3" in ids, ids)
    check("3 消したデッキは復活しない", "d_2" not in ids, ids)
    check("3 合流待ちの印が消える", page.evaluate("() => localStorage.getItem('gl_handoff_pending')") is None)
    ctx.close()

    # ── 4. 新ドメインを先に使っていた人の初回: 合流する・同期の状態は新側を残す ──
    ctx = browser.new_context()
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    seed(page, NEW, {"cardprice_saved_decks": json.dumps([deck(10, "新側", 1)]),
                     "cardprice_wishlist": json.dumps([{"name": "増殖するG", "qty": 1, "rarity": ""}])})
    seed(page, OLD, {"cardprice_saved_decks": json.dumps([deck(20, "旧側", 2)]),
                     "cardprice_wishlist": json.dumps([{"name": "灰流うらら", "qty": 1, "rarity": ""}]),
                     "cardprice_sync_state": json.dumps({"sync_id": "22222222-2222-2222-2222-222222222222", "wishlist_rev": 1, "decks_rev": 1}),
                     "sol-toggle-seen": "1"})
    page.goto(OLD + "/")
    wait_new(page)
    page.wait_for_load_state("load")
    page.wait_for_timeout(800)
    st = page.evaluate("() => ({d: JSON.parse(localStorage.getItem('cardprice_saved_decks')).map(x=>x.id), w: JSON.parse(localStorage.getItem('cardprice_wishlist')).map(x=>x.name), s: localStorage.getItem('cardprice_sync_state'), t: localStorage.getItem('sol-toggle-seen')})")
    check("4 デッキが合流", set(st["d"]) == {"d_10", "d_20"}, st)
    check("4 購入候補が合流", set(st["w"]) == {"増殖するG", "灰流うらら"}, st)
    check("4 旧側の同期の状態は写さない", not st["s"] or "22222222" not in st["s"], st)
    check("4 その他の設定は足される", st["t"] == "1", st)
    ctx.close()

    # ── 5. 他のサイトからの細工リンク: 受け取らない・旧URLの /handoff は無い ──
    ctx = browser.new_context()
    ctx2 = browser.new_context()
    victim = ctx2.new_page()
    page = ctx.new_page()
    page.goto(NEW + "/robots.txt")
    payload_url = page.evaluate("""async () => {
        const items = {cardprice_sync_state: JSON.stringify({sync_id: '33333333-3333-3333-3333-333333333333'}), cardprice_saved_decks: '[]'};
        const bytes = new TextEncoder().encode(JSON.stringify({v:2, items}));
        let s=''; bytes.forEach(b => s += String.fromCharCode(b));
        return 'j.' + btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
    }""")
    victim.goto(NEW + "/handoff?next=/#" + payload_url, referer="https://evil.example/")
    victim.wait_for_timeout(800)
    check("5 他サイト発は受け取らない", victim.evaluate("() => localStorage.getItem('cardprice_sync_state')") is None)
    check("5 案内が出る", "引き継げません" in victim.inner_text("body"), victim.inner_text("body")[:80])
    res = victim.goto(OLD + "/handoff?next=/handoff%23" + payload_url)
    check("5 旧URLの /handoff は 404", res.status == 404, res.status)

    # ── 6. 送り元が分からない: 確認してから・同期の状態は写さない ──
    victim.goto("about:blank")
    victim.evaluate("() => 0")
    victim.goto(NEW + "/handoff?next=/#" + payload_url)
    victim.wait_for_timeout(500)
    check("6 確認ボタンが出る", victim.is_visible("#handoffConfirm"))
    check("6 押す前は何も書かない", victim.evaluate("() => localStorage.length") == 0)
    victim.click("#handoffConfirm")
    victim.wait_for_url(NEW + "/", timeout=10000)
    victim.wait_for_timeout(500)
    check("6 押した後も同期の状態は写さない", victim.evaluate("() => localStorage.getItem('cardprice_sync_state')") is None)
    ctx.close(); ctx2.close()

    # ── 7. 旧版の拡張機能の取り込み（?tcgym_import=1）: 画面が閉じるまで待ち、取り込んだデッキも運ぶ ──
    ctx = browser.new_context()
    ctx.add_init_script("""
      if (location.origin === 'http://localhost:5099' && new URLSearchParams(location.search).has('tcgym_import')) {
        window.addEventListener('DOMContentLoaded', () => {
          setTimeout(() => {
            history.replaceState(null, '', location.pathname);
            const o = document.createElement('div'); o.className = 'tdi-overlay'; document.body.appendChild(o);
            setTimeout(() => {
              const l = JSON.parse(localStorage.getItem('cardprice_saved_decks') || '[]');
              l.push({id: 'd_ext', name: '拡張', text: '1 x', main: [{qty:1,name:'x'}], ex: [], updated: Date.now()});
              localStorage.setItem('cardprice_saved_decks', JSON.stringify(l));
              o.remove(); location.reload();
            }, 2500);
          }, 300);
        });
      }""")
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    t0 = time.time()
    page.goto(OLD + "/?tcgym_import=1")
    wait_new(page, 20000)
    page.wait_for_timeout(800)
    ids = [d["id"] for d in page.evaluate("() => JSON.parse(localStorage.getItem('cardprice_saved_decks') || '[]')")]
    check("7 拡張で取り込んだデッキが新ドメインに届く", "d_ext" in ids, ids)
    check("7 取り込み画面の途中で移動しない", time.time() - t0 > 2.5)
    ctx.close()

    # ── 8. 拡張機能が無いのに ?tcgym_import=1: 数秒で移る ──
    ctx = browser.new_context()
    page = ctx.new_page()
    t0 = time.time()
    page.goto(OLD + "/?tcgym_import=1")
    wait_new(page, 20000)
    check("8 拡張なしでも移る（5秒程度）", time.time() - t0 < 9, round(time.time() - t0, 1))
    check("8 引数は保ったまま新ドメインへ", page.url == NEW + "/?tcgym_import=1", page.url)
    ctx.close()

    # ── 9. 受け取りに失敗しても、次に旧URLを開けば全部届く ──
    ctx = browser.new_context()
    page = ctx.new_page()
    seed(page, OLD, {"cardprice_saved_decks": json.dumps([deck(40, "失敗後", 1)]),
                     "cardprice_wishlist": json.dumps([{"name": "灰流うらら", "qty": 1, "rarity": ""}])})
    page.route("**/handoff?*", lambda route: route.abort())
    try:
        page.goto(OLD + "/")
        page.wait_for_timeout(2000)
    except Exception:
        pass
    page.unroute("**/handoff?*")
    page.goto(OLD + "/")
    wait_new(page)
    page.wait_for_timeout(500)
    st = page.evaluate("() => ({d: JSON.parse(localStorage.getItem('cardprice_saved_decks') || '[]').map(x => x.id), w: JSON.parse(localStorage.getItem('cardprice_wishlist') || '[]').map(x => x.name)})")
    check("9 失敗のあと開き直すとデッキ・購入候補が届く", "d_40" in st["d"] and "灰流うらら" in st["w"], st)
    ctx.close()

    check("ページのエラーなし", not errors, errors[:3])
    browser.close()

ng = [r for r in results if not r[1]]
print(f"\n{len(results) - len(ng)}/{len(results)} OK")
sys.exit(1 if ng else 0)
