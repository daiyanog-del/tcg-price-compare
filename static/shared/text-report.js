/**
 * text-report.js — 未発売カードの「テキストの誤りを報告」ボタンとフォーム
 *
 * 未発売カード（/api/card-info が kind:'proxy' を返すもの）の詳細に置く専用ボタン。
 * カードページ（index-main.js）とシミュレーター（card-info-panel.js・mobile-ui.js）の両方から使う共通部品。
 * 送信先: POST /api/card-text-report {name, field, note}
 *
 * - 通常の script として読み込み、window.TextReport を公開する（ES モジュール側からは window 経由で呼ぶ）
 * - 画面に出す値はすべて textContent で設定する（innerHTML は使わない）
 * - インラインの onclick は使わず addEventListener で配線する
 * - フォームはページに1つだけ作り、開くたびに対象カード名を差し替える
 */
(function () {
  'use strict';

  var API = '/api/card-text-report';
  var NOTE_MAX = 500;
  // 誤りのある欄（サーバーの許可リスト unreleased_text_report.FIELD_LABELS と揃える）
  var FIELDS = [
    ['effect_text', '効果テキスト'],
    ['pendulum_effect', 'ペンデュラム効果'],
    ['card_name', 'カード名'],
    ['stats', '種族・属性・数値'],
    ['other', 'その他'],
  ];

  var dlg = null;        // フォームの要素一式（初回に作る）
  var currentName = '';
  var lastFocus = null;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function build() {
    var overlay = el('div', 'tr-overlay');
    overlay.hidden = true;
    var backdrop = el('div', 'tr-backdrop');
    var box = el('div', 'tr-dialog');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', 'trTitle');

    var head = el('div', 'tr-head');
    var title = el('h2', 'tr-title', 'テキストの誤りを報告');
    title.id = 'trTitle';
    var close = el('button', 'tr-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', '閉じる');
    head.appendChild(title);
    head.appendChild(close);

    var body = el('div', 'tr-body');
    var nameEl = el('p', 'tr-card-name');

    var selLabel = el('label', 'tr-label', 'どこが違いますか');
    selLabel.htmlFor = 'trField';
    var select = el('select', 'tr-select');
    select.id = 'trField';
    FIELDS.forEach(function (f) {
      var o = el('option', null, f[1]);
      o.value = f[0];
      select.appendChild(o);
    });

    var noteLabel = el('label', 'tr-label', 'ひとこと（任意）');
    noteLabel.htmlFor = 'trNote';
    var note = el('textarea', 'tr-textarea');
    note.id = 'trNote';
    note.maxLength = NOTE_MAX;
    note.rows = 4;
    note.placeholder = '例: 「墓地へ返って」は「墓地へ送って」です';
    var count = el('div', 'tr-count', '0 / ' + NOTE_MAX);

    var status = el('p', 'tr-status');
    status.setAttribute('role', 'status');
    status.hidden = true;

    body.appendChild(nameEl);
    body.appendChild(selLabel);
    body.appendChild(select);
    body.appendChild(noteLabel);
    body.appendChild(note);
    body.appendChild(count);
    body.appendChild(status);

    var foot = el('div', 'tr-foot');
    var cancel = el('button', 'tr-cancel', 'キャンセル');
    cancel.type = 'button';
    var submit = el('button', 'tr-submit', '報告する');
    submit.type = 'button';
    foot.appendChild(cancel);
    foot.appendChild(submit);

    box.appendChild(head);
    box.appendChild(body);
    box.appendChild(foot);
    overlay.appendChild(backdrop);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    var d = { overlay: overlay, nameEl: nameEl, select: select, note: note, count: count,
              status: status, submit: submit, cancel: cancel, sent: false };
    backdrop.addEventListener('click', closeDialog);
    close.addEventListener('click', closeDialog);
    cancel.addEventListener('click', closeDialog);
    note.addEventListener('input', function () {
      count.textContent = note.value.length + ' / ' + NOTE_MAX;
    });
    submit.addEventListener('click', function () { send(d); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !overlay.hidden) closeDialog();
    });
    return d;
  }

  function showStatus(msg, ok) {
    dlg.status.textContent = msg;
    dlg.status.className = 'tr-status ' + (ok ? 'tr-status--ok' : 'tr-status--err');
    dlg.status.hidden = false;
  }

  function openDialog(name) {
    if (!dlg) dlg = build();
    currentName = String(name || '');
    lastFocus = document.activeElement;
    dlg.nameEl.textContent = currentName;
    dlg.select.value = FIELDS[0][0];
    dlg.note.value = '';
    dlg.count.textContent = '0 / ' + NOTE_MAX;
    dlg.status.hidden = true;
    dlg.status.textContent = '';
    dlg.sent = false;
    dlg.submit.disabled = false;
    dlg.submit.textContent = '報告する';
    dlg.cancel.hidden = false;
    dlg.overlay.hidden = false;
    dlg.select.focus();
  }

  function closeDialog() {
    if (!dlg) return;
    dlg.overlay.hidden = true;
    if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
  }

  function send(d) {
    if (d.sent) { closeDialog(); return; }
    var note = d.note.value.trim();
    if (note.length > NOTE_MAX) {
      showStatus('ひとことは' + NOTE_MAX + '文字以内にしてください', false);
      return;
    }
    d.submit.disabled = true;
    d.status.hidden = true;
    fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: currentName, field: d.select.value, note: note }),
    }).then(function (res) {
      if (res.status === 429) {
        showStatus('短時間に送りすぎです。少し待ってからもう一度お試しください', false);
        d.submit.disabled = false;
        return;
      }
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.ok && data.ok) {
          d.sent = true;
          showStatus('報告を受け付けました。ありがとうございます', true);
          d.submit.textContent = '閉じる';
          d.submit.disabled = false;
          d.cancel.hidden = true;
        } else {
          showStatus(data.error || '送信できませんでした', false);
          d.submit.disabled = false;
        }
      });
    }).catch(function () {
      showStatus('通信エラーが発生しました', false);
      d.submit.disabled = false;
    });
  }

  /**
   * container の末尾に「テキストの誤りを報告」ボタンを置く。
   * 同じ container に既にあれば置き直す（再描画で二重にならない）。
   * 名前が空のときは何も置かない（誰のカードか分からない報告は受け付けられない）。
   */
  function attach(container, name) {
    if (!container) return null;
    var old = container.querySelectorAll('.tr-open-wrap');
    for (var i = 0; i < old.length; i++) old[i].remove();
    if (!name) return null;
    var wrap = el('div', 'tr-open-wrap');
    var btn = el('button', 'tr-open-btn', 'テキストの誤りを報告');
    btn.type = 'button';
    btn.addEventListener('click', function () { openDialog(name); });
    wrap.appendChild(btn);
    container.appendChild(wrap);
    return btn;
  }

  window.TextReport = { attach: attach, open: openDialog };
})();
