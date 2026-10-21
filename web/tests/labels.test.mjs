// Подписи «на месте» и строка времени многодневного события, ru/kk/en. Запуск: node --test "web/tests/*.test.mjs"
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { eventStatus, eventWhen } from '../events.js';
import { setLang, t } from '../i18n.js';
import { visitLabel } from '../visit.js';

const say = (lang, label) => {
  setLang(lang);
  return t(label.key, label.vars);
};

test('равные границы времени на месте — одно число, ru/kk/en', () => {
  assert.equal(say('ru', visitLabel(105, 105)), 'на месте 105 мин');
  assert.equal(say('kk', visitLabel(105, 105)), 'орнында 105 мин');
  assert.equal(say('en', visitLabel(105, 105)), '105 min there');
});

test('разные границы — диапазон, ru/kk/en', () => {
  assert.equal(say('ru', visitLabel(60, 90)), 'на месте 60–90 мин');
  assert.equal(say('kk', visitLabel(60, 90)), 'орнында 60–90 мин');
  assert.equal(say('en', visitLabel(60, 90)), '60–90 min there');
});

const expo = { kind: 'event', starts_at: '2026-10-01T10:00:00+05:00', ends_at: '2026-11-30T18:00:00+05:00' };
const now = '2026-10-21T23:00:00+05:00';

test('многодневное событие — диапазон дат, а не «01.10 в 10:00», ru/kk/en', () => {
  for (const lang of ['ru', 'kk', 'en']) {
    assert.equal(say(lang, eventWhen(expo, now)), '01.10 10:00 – 30.11 18:00');
  }
});

test('плашка многодневного события остаётся: «проходит до 30.11 18:00», ru/kk/en', () => {
  assert.equal(say('ru', eventStatus(expo, now)), 'проходит до 30.11 18:00');
  assert.equal(say('kk', eventStatus(expo, now)), '30.11 18:00 дейін жалғасады');
  assert.equal(say('en', eventStatus(expo, now)), 'runs until 30.11 18:00');
});

test('сеанс через полночь (≤ 12 ч) — без диапазона дат: «сегодня в 22:00»', () => {
  const concert = { kind: 'event', starts_at: '2026-10-21T22:00:00+05:00', ends_at: '2026-10-22T02:00:00+05:00' };
  assert.equal(say('ru', eventWhen(concert, now)), 'сегодня в 22:00');
});

test('сеанс в один день — «сегодня в 19:00–21:00», завтра — «завтра в …», ru/kk/en', () => {
  const ev = { kind: 'event', starts_at: '2026-10-21T19:00:00+05:00', ends_at: '2026-10-21T21:00:00+05:00' };
  assert.equal(say('ru', eventWhen(ev, '2026-10-21T15:30:00+05:00')), 'сегодня в 19:00–21:00');
  assert.equal(say('kk', eventWhen(ev, '2026-10-21T15:30:00+05:00')), 'бүгін 19:00–21:00');
  assert.equal(say('en', eventWhen(ev, '2026-10-20T15:30:00+05:00')), 'tomorrow at 19:00–21:00');
});
