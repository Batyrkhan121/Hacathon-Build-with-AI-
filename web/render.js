// Отрисовка результатов. Все тексты с сервера вставляем через textContent: это данные, не разметка.
import { t } from './i18n.js';

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function renderIntro(root) {
  root.replaceChildren(h('p', { class: 'intro', text: t('intro') }));
}

export function renderLoading(root) {
  root.replaceChildren(h('p', { class: 'intro', text: t('submit.busy') }));
}

export function errorText(err) {
  return `${err.kind}${err.status ? ` ${err.status}` : ''}`;
}

export function renderError(root, err) {
  root.replaceChildren(h('p', { class: 'intro', text: errorText(err) }));
}

export function renderAnswer(root, answer) {
  root.replaceChildren(
    h('p', { class: 'summary', text: answer.summary }),
    h('ol', {}, answer.cards.map((c) => h('li', { text: c.title }))),
  );
}
