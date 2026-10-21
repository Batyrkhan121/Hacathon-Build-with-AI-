// Время события по Астане (UTC+5) без DOM и без i18n, чтобы проверять в node (web/tests/events.test.mjs).
// Подписи — ключ i18n + параметры; переводит render.js. Правило — docs/ARCHITECTURE.md §5:
// «идёт до» — только у сеанса (ends_at − starts_at ≤ 12 ч) и только при starts_at ≤ сейчас < ends_at;
// у события дольше 12 ч (выставка, фестиваль) — «проходит до 30.11 18:00»; без ends_at «идёт» не утверждаем —
// «началось в 19:00»; время на другой день — всегда с датой.

export const ASTANA_OFFSET_MS = 5 * 3600 * 1000;
export const SESSION_MAX_MS = 12 * 3600 * 1000;

export function localParts(iso) {
  // Переводим момент в UTC+5 (строка может прийти с любым смещением), а не режем ISO-строку
  const d = new Date(Date.parse(iso) + ASTANA_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, '0');
  return {
    date: `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`,
    time: `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`,
  };
}

export function dayDiff(dateA, dateB) {
  return Math.round((Date.parse(`${dateA}T00:00:00Z`) - Date.parse(`${dateB}T00:00:00Z`)) / 86400000);
}

// «02:00» в тот же день, что nowIso, иначе «22.10 02:00»
export function clockAt(iso, nowIso) {
  const at = localParts(iso);
  if (nowIso && at.date === localParts(nowIso).date) return at.time;
  const [, m, d] = at.date.split('-');
  return `${d}.${m} ${at.time}`;
}

// Подпись уже начавшегося события или null (ещё не началось, уже кончилось, нет данных).
// live — сеанс идёт прямо сейчас (зелёная плашка); иначе — нейтральная.
export function eventStatus(card, nowIso) {
  if (card.kind !== 'event' || !card.starts_at || !nowIso) return null;
  const now = Date.parse(nowIso);
  const start = Date.parse(card.starts_at);
  if (Number.isNaN(now) || Number.isNaN(start) || start > now) return null;
  if (!card.ends_at) {
    const at = localParts(card.starts_at);
    if (at.date === localParts(nowIso).date) return { key: 'event.started', vars: { time: at.time }, live: false };
    const [, m, d] = at.date.split('-');
    return { key: 'event.startedDated', vars: { date: `${d}.${m}`, time: at.time }, live: false };
  }
  const end = Date.parse(card.ends_at);
  if (Number.isNaN(end) || now >= end) return null;
  const when = clockAt(card.ends_at, nowIso);
  if (end - start > SESSION_MAX_MS) return { key: 'event.runsUntil', vars: { when }, live: false };
  return { key: 'event.until', vars: { when }, live: true };
}

// Остановка-событие в таймлайне: начавшееся — eventStatus, иначе «к 19:00» (с датой, если не сегодня)
export function eventStopLabel(card, nowIso) {
  return eventStatus(card, nowIso) || { key: 'event.by', vars: { time: clockAt(card.starts_at, nowIso) }, live: false };
}
