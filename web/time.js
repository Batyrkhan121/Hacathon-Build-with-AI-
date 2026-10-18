// Время Астаны (UTC+5 круглый год) без зависимости от часового пояса браузера.
// Сдвигаем «сейчас» на +5 ч и дальше читаем дату в UTC.
import { locale } from './i18n.js';

const OFFSET_MS = 5 * 3600 * 1000;
const DAY_MS = 24 * 3600 * 1000;

export function astanaNow(dayOffset = 0) {
  return new Date(Date.now() + OFFSET_MS + dayOffset * DAY_MS);
}

export function astanaDate(dayOffset = 0) {
  return astanaNow(dayOffset).toISOString().slice(0, 10); // YYYY-MM-DD
}

export function astanaClock() {
  return astanaNow().toISOString().slice(11, 16); // HH:MM
}

// `at` для RecommendRequest: null = «сейчас» на сервере; иначе ISO с +05:00.
export function atIso(time, dayOffset = 0) {
  if (!time && !dayOffset) return null;
  return `${astanaDate(dayOffset)}T${time || astanaClock()}:00+05:00`;
}

// Через сколько дней от сегодняшнего (по Астане) наступает `at` из запроса.
export function dayOffsetOf(at) {
  if (!at) return 0;
  const target = Date.parse(`${at.slice(0, 10)}T00:00:00Z`);
  const today = Date.parse(`${astanaDate()}T00:00:00Z`);
  return Math.round((target - today) / DAY_MS);
}

// «пн 19.10» на языке интерфейса.
export function shortDay(dayOffset = 0) {
  const d = astanaNow(dayOffset);
  const weekday = new Intl.DateTimeFormat(locale(), { weekday: 'short', timeZone: 'UTC' }).format(d);
  const dm = new Intl.DateTimeFormat(locale(), { day: '2-digit', month: '2-digit', timeZone: 'UTC' }).format(d);
  return `${weekday.replace(/\.$/, '')} ${dm}`;
}
