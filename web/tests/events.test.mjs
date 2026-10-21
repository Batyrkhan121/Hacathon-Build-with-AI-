// Подписи времени события (ARCHITECTURE §5). Запуск: node --test "web/tests/*.test.mjs"
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { clockAt, eventStatus, eventStopLabel } from '../events.js';

const ev = (starts_at, ends_at = null) => ({ kind: 'event', starts_at, ends_at });

test('выставка 01.10 10:00–30.11 18:00 в 23:00 — «проходит до 30.11 18:00», не «идёт до 18:00»', () => {
  const st = eventStatus(ev('2026-10-01T10:00:00+05:00', '2026-11-30T18:00:00+05:00'), '2026-10-21T23:00:00+05:00');
  assert.deepEqual(st, { key: 'event.runsUntil', vars: { when: '30.11 18:00' }, live: false });
});

test('концерт через полночь 22:00–02:00 в 23:00 — «идёт до 22.10 02:00»', () => {
  const st = eventStatus(ev('2026-10-21T22:00:00+05:00', '2026-10-22T02:00:00+05:00'), '2026-10-21T23:00:00+05:00');
  assert.deepEqual(st, { key: 'event.until', vars: { when: '22.10 02:00' }, live: true });
});

test('сеанс в тот же день — «идёт до 21:00» без даты', () => {
  const st = eventStatus(ev('2026-10-21T19:00:00+05:00', '2026-10-21T21:00:00+05:00'), '2026-10-21T20:00:00+05:00');
  assert.deepEqual(st, { key: 'event.until', vars: { when: '21:00' }, live: true });
});

test('ровно 12 ч — ещё сеанс; 12 ч 1 мин — уже «проходит до»', () => {
  const now = '2026-10-21T12:00:00+05:00';
  assert.equal(eventStatus(ev('2026-10-21T10:00:00+05:00', '2026-10-21T22:00:00+05:00'), now).key, 'event.until');
  assert.equal(eventStatus(ev('2026-10-21T10:00:00+05:00', '2026-10-21T22:01:00+05:00'), now).key, 'event.runsUntil');
});

test('без ends_at «идёт» не утверждаем — «началось в 19:00»', () => {
  const st = eventStatus(ev('2026-10-21T19:00:00+05:00'), '2026-10-21T20:30:00+05:00');
  assert.deepEqual(st, { key: 'event.started', vars: { time: '19:00' }, live: false });
});

test('без ends_at, началось вчера — с датой', () => {
  const st = eventStatus(ev('2026-10-20T19:00:00+05:00'), '2026-10-21T01:00:00+05:00');
  assert.deepEqual(st, { key: 'event.startedDated', vars: { date: '20.10', time: '19:00' }, live: false });
});

test('ещё не началось или уже кончилось — подписи нет', () => {
  const now = '2026-10-21T15:30:00+05:00';
  assert.equal(eventStatus(ev('2026-10-21T19:00:00+05:00', '2026-10-21T21:00:00+05:00'), now), null);
  assert.equal(eventStatus(ev('2026-10-21T10:00:00+05:00', '2026-10-21T15:30:00+05:00'), now), null);
  assert.equal(eventStatus({ kind: 'place', starts_at: '2026-10-21T10:00:00+05:00' }, now), null);
});

test('время из строки с другим смещением переводится в UTC+5', () => {
  const st = eventStatus(ev('2026-10-21T14:00:00Z', '2026-10-21T16:00:00Z'), '2026-10-21T20:00:00+05:00');
  assert.deepEqual(st.vars, { when: '21:00' });
});

test('остановка-событие до начала — «к 19:00», на другой день — с датой', () => {
  const now = '2026-10-21T18:45:00+05:00';
  assert.deepEqual(eventStopLabel(ev('2026-10-21T19:00:00+05:00'), now), {
    key: 'event.by',
    vars: { time: '19:00' },
    live: false,
  });
  assert.equal(clockAt('2026-10-22T19:00:00+05:00', now), '22.10 19:00');
});
