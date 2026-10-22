"""一人回しの modulepreload 一覧（module_graph）とテンプレートへの出力を検証する。"""
import re

import module_graph


def _write(root, rel, text):
    path = root.joinpath(*rel.split('/'))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def test_follows_static_imports_but_not_dynamic_or_external(tmp_path):
    _write(tmp_path, 'app/main.js', """
import { a } from './a.js';
import {
  b,
} from "./sub/b.js";
import './side.js';
export { c } from './c.js';
import x from 'https://cdn.example/x.js';
const lazy = () => import('./lazy.js');
""")
    _write(tmp_path, 'app/a.js', "import { s } from '../shared/s.js';\n")
    _write(tmp_path, 'app/sub/b.js', "import { a } from '../a.js';\n")
    for rel in ['app/side.js', 'app/c.js', 'app/lazy.js', 'shared/s.js']:
        _write(tmp_path, rel, '')
    deps = module_graph.module_dependencies(str(tmp_path), 'app/main.js')
    assert deps == ['app/a.js', 'app/sub/b.js', 'app/side.js', 'app/c.js', 'shared/s.js']


def test_ignores_missing_files_and_paths_outside_root(tmp_path):
    _write(tmp_path, 'static/main.js', "import './gone.js';\nimport '../../outside.js';\n")
    _write(tmp_path, 'outside.js', '')
    assert module_graph.module_dependencies(str(tmp_path / 'static'), 'main.js') == []


def test_solitaire_page_preloads_every_statically_imported_module():
    import app as app_module
    html = app_module.app.test_client().get('/solitaire').get_data(as_text=True)
    preloads = re.findall(r'<link rel="modulepreload" href="([^"]+)">', html)
    deps = module_graph.module_dependencies(app_module.app.static_folder, 'solitaire/js/main.js')
    assert len(deps) > 30
    # 入口 main.js ＋ 依存すべてが、import map と同じ内容ハッシュ付きURLで並ぶ
    with app_module.app.test_request_context():
        expected = [app_module.static_url(p) for p in ['solitaire/js/main.js', *deps]]
    assert preloads == expected
    assert all('?v=' in url for url in preloads)
    # 先読みURLは import map の写像先と一致しないと二重取得になる
    import json
    raw = html[html.index('type="importmap">') + len('type="importmap">'):]
    importmap_urls = set(json.loads(raw[:raw.index('</script>')])['imports'].values())
    assert set(preloads) <= importmap_urls
    importmap = html.index('type="importmap"')
    assert importmap < html.index('rel="modulepreload"')
    # 旧 main.css の @import 直列は無くなり、個別 <link> が順に並ぶ
    assert 'solitaire/css/main.css' not in html
    order = [html.index(f'solitaire/css/{n}.css') for n in
             ['variables', 'layout', 'components', 'controls', 'modal', 'solitaire-brand',
              'proxy-card', 'responsive', 'mobile', 'comments']]
    assert order == sorted(order)


def test_follows_site_root_static_imports(tmp_path):
    """'/static/...' から始まる指定もたどる（2026-10-22: シミュレーターで neuron の2ファイルが先読みから漏れていた）。"""
    _write(tmp_path, 'app/main.js', "import { p } from '/static/shared/p.js';\nimport x from '/other/x.js';\n")
    _write(tmp_path, 'shared/p.js', "import { q } from './q.js';\n")
    _write(tmp_path, 'shared/q.js', '')
    deps = module_graph.module_dependencies(str(tmp_path), 'app/main.js')
    assert deps == ['shared/p.js', 'shared/q.js']


def test_solitaire_preloads_neuron_modules():
    import app as app_module
    html = app_module.app.test_client().get('/solitaire').get_data(as_text=True)
    preloads = re.findall(r'<link rel="modulepreload" href="([^"]+)">', html)
    assert any('shared/neuron-pdf-parser.js' in u for u in preloads)
    assert any('shared/neuron-preview-modal.js' in u for u in preloads)


def test_follows_minified_imports(tmp_path):
    """本番のイメージでは JS を縮小する（Dockerfile の assets 段）。空白の無い import / export も拾う（2026-10-22）。"""
    _write(tmp_path, 'app/main.js',
           'import{a as b}from"./a.js";import"./side.js";export{c}from"./c.js";const l=()=>import("./lazy.js");')
    for rel in ['app/a.js', 'app/side.js', 'app/c.js', 'app/lazy.js']:
        _write(tmp_path, rel, '')
    deps = module_graph.module_dependencies(str(tmp_path), 'app/main.js')
    assert deps == ['app/a.js', 'app/side.js', 'app/c.js']
