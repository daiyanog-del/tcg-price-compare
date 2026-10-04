"""外部通信なしで店舗検索の共有による資源上限を比較する。

固定HTML解析と20msの疑似待機を使用する。本番スクレイピングの性能測定でも、
最適な並列数の推定でもない。同時実行上限と重複抑制の回帰検証用。
旧方式は同時カードごとに店舗 executor を生成するファンアウトを再現する。
リクエスト／カードの制御スレッドは含めないため、メモリ比較も限定的である。
"""
import json
import sys
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from html.parser import HTMLParser
from pathlib import Path
from threading import Event, Lock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shop_search_pool import ShopSearchPool


HTML = '<table>' + '<tr><td>青眼の白龍</td><td>1200</td></tr>' * 30 + '</table>'


class PriceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.prices = []

    def handle_data(self, data):
        if data.isdecimal():
            self.prices.append(int(data))


def runner(fn, name):
    return fn(name)


def measure(shared):
    release = Event()
    lock = Lock()
    active = peak = calls = 0

    def scrape(name, shop):
        nonlocal active, peak, calls
        with lock:
            active += 1
            peak = max(peak, active)
            calls += 1
        try:
            if not release.wait(5):
                raise TimeoutError('測定の開始待機が終了しませんでした')
            time.sleep(0.02)  # 疑似通信の固定入力。運用パラメータではない。
            parser = PriceParser()
            parser.feed(HTML)
            return name, shop, min(parser.prices)
        finally:
            with lock:
                active -= 1

    # 同じ3カードを2リクエストが同時検索する固定シナリオ。
    shops = [partial(scrape, shop=shop) for shop in range(6)]
    batches = [[('カード' + str(card), fn) for fn in shops]
               for request in range(2) for card in range(3)]
    executors = []
    tracemalloc.start()
    started = time.perf_counter()
    try:
        if shared:
            pool = ShopSearchPool(max_workers=6, max_pending=30)
            executors.append(pool)
            futures = [pool.submit(fn, name, runner=runner) for batch in batches for name, fn in batch]
        else:
            futures = []
            for batch in batches:
                executor = ThreadPoolExecutor(max_workers=6)
                executors.append(executor)
                futures.extend(executor.submit(runner, fn, name) for name, fn in batch)
        release.set()
        results = [future.result(timeout=5) for future in futures]
    finally:
        release.set()
        for executor in executors:
            executor.shutdown(wait=True)
    elapsed = time.perf_counter() - started
    _, memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(results) == 36
    assert all(price == 1200 for _, _, price in results)
    if shared:
        assert peak <= 6 and calls == 18
    else:
        assert calls == 36
    return {
        '方式': '共有プール' if shared else '従来のカード別プール',
        '依頼数': len(results), '店舗処理実行回数': calls,
        '最大同時実行数': peak, '所要秒': round(elapsed, 4),
        'Python割当ピークKiB': round(memory / 1024, 1),
    }


if __name__ == '__main__':
    print('外部通信なしの資源上限検証。本番性能や最適値を示す測定ではありません。')
    print(json.dumps([measure(False), measure(True)], ensure_ascii=False, indent=2))
