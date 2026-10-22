"""ES module の静的 import をたどり、入口から読まれるファイルの一覧を返す。

一人回し（solitaire.html）は main.js から相対 import で多数のファイルを読む。ブラウザは
各ファイルを読み終えてから次の import に気づくため、依存の深さぶん往復が直列になる
（低速端末で初期化約4秒の一因・2026-10-16 実測）。ここで依存を前もって列挙し、
テンプレートが <link rel="modulepreload"> で一度に先読みさせる。

対象は静的 import / export ... from だけ。動的 import() は「必要になってから読む」意図なので含めない。
"""
import os
import re

# import x from './a.js' / import {a} from "./a.js" / import './a.js' / export {a} from './a.js'
# （複数行にまたがる import { ... } from も拾う。文字列中やコメント中の偽一致は、
#   実在しないファイルなら除外されるので害はない）
# 本番のイメージでは JS を縮小する（Dockerfile の assets 段）ので、空白の無い import{a}from"./a.js"; も拾う（2026-10-22）
_IMPORT_RE = re.compile(
    r"""(?:^|[;\s}])(?:import|export)\s*(?:[^'"();]*?\s*from\s*)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def module_dependencies(static_root: str, entry: str) -> list[str]:
    """入口 entry（static_root からの相対パス）から静的 import でたどれるファイルを返す。

    戻り値は static_root からの相対パス（区切りは '/'）。入口自身は含めない。
    順序は幅優先（浅い依存から）。static_root の外や実在しないファイルは無視する。
    """
    root = os.path.realpath(static_root)
    seen: set[str] = {entry}
    order: list[str] = []
    queue = [entry]
    while queue:
        current = queue.pop(0)
        try:
            with open(os.path.join(root, *current.split('/')), encoding='utf-8') as f:
                source = f.read()
        except OSError:
            continue
        base_dir = os.path.dirname(current)
        for spec in _IMPORT_RE.findall(source):
            if spec.startswith('/static/'):
                # サイトの根からの指定（例 '/static/shared/neuron-pdf-parser.js'）。static_root からの相対に直す。
                # 以前はたどらず、シミュレーターでこの2ファイルが先読みから漏れ、JS の実行開始が約0.2秒遅れていた（2026-10-22）
                rel = os.path.normpath(spec[len('/static/'):]).replace(os.sep, '/')
            elif spec.startswith(('./', '../')):
                rel = os.path.normpath(os.path.join(base_dir, spec)).replace(os.sep, '/')
            else:
                continue  # 外部URL・裸の指定子は対象外
            abs_path = os.path.realpath(os.path.join(root, *rel.split('/')))
            if os.path.commonpath([root, abs_path]) != root or not os.path.isfile(abs_path):
                continue
            if rel in seen:
                continue
            seen.add(rel)
            order.append(rel)
            queue.append(rel)
    return order
