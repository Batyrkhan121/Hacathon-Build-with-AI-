// Local fixture copy only. Facts and the user's original query are never rewritten.
// Exact translations avoid deriving a place name or opening schedule from its category.
const TEXT = [
  ['Alem.ai — Сингулярлық музейі', 'Alem.ai — Музей Сингулярности', 'Alem.ai — Museum of Singularity'],
  ['Балалар алаңы · 120 м', 'Детская площадка · 120 м', 'Playground · 120 m'],
  ['Музей · демо орын', 'Музей · демо-место', 'Museum · demo place'],
  ['EXPO маңындағы интерактивті музей — танымдық серуенге сай.', 'Интерактивный музей рядом с EXPO для познавательной прогулки.', 'An interactive museum near EXPO for a curious walk.'],
  ['Музей экспозициясын қараңыз.', 'Посмотрите экспозицию музея.', 'Explore the museum exhibits.'],
  ['Жақын жердегі жылы аялдама. Ауа райы салқын болса, ғимарат ішінде демалуға болады.', 'Тёплая остановка рядом. В прохладную погоду можно отдохнуть внутри.', 'A warm stop nearby. Take a break indoors if the weather is cold.'],
  ['Галереяны аралап, тамақтанатын орынды таңдаңыз.', 'Пройдитесь по галерее и выберите место для перекуса.', 'Walk through the gallery and choose somewhere to eat.'],
  ['Баламен қысқа үзіліске арналған жақын алаң.', 'Площадка рядом для короткой остановки с ребёнком.', 'A nearby playground for a short break with children.'],
  ['Алаңның қолжетімділігін сол жерде тексеріп, аздап демалыңыз.', 'Проверьте доступность площадки на месте и немного отдохните.', 'Check access to the playground when you arrive and take a short break.'],
  ['Мок-кесте: сейсенбі 10:00–18:30.', 'Мок-расписание: вторник, 10:00–18:30.', 'Mock schedule: Tuesday, 10:00–18:30.'],
  ['Мок: сейсенбі 10:00–18:30; бұл расталған кесте емес.', 'Мок: вторник, 10:00–18:30; расписание не подтверждено.', 'Mock: Tuesday, 10:00–18:30; the schedule is not confirmed.'],
  ['Мок: сейсенбі 10:00–18:30; жұмыс уақыты расталмаған.', 'Мок: вторник, 10:00–18:30; часы работы не подтверждены.', 'Mock: Tuesday, 10:00–18:30; opening hours are not confirmed.'],
  ['Мок: күн сайын 10:00–22:00; жұмыс уақыты расталмаған.', 'Мок: ежедневно, 10:00–22:00; часы работы не подтверждены.', 'Mock: daily, 10:00–22:00; opening hours are not confirmed.'],
  ['Жұмыс уақыты туралы дерек жоқ, барар алдында тексеріңіз.', 'Нет данных о часах работы, проверьте перед визитом.', 'No opening-hours data; check before you visit.'],
  ['Мок: 40 минуттан кейін жабылады (18:10).', 'Мок: закроется через 40 минут (18:10).', 'Mock: closes in 40 minutes (18:10).'],
  ['Мок: жұмыс кестесі толық расталмаған, жабық болуы мүмкін.', 'Мок: расписание полностью не подтверждено; возможно, закрыто.', 'Mock: the schedule is not fully confirmed; the place may be closed.'],
  ['Мок-мекенжай · EXPO', 'Мок-адрес · EXPO', 'Mock address · EXPO'],
  ['Мок-мекенжай', 'Мок-адрес', 'Mock address'],
  ['EXPO маңында музей, жылы үзіліс және балалар алаңы.', 'Музей, тёплая остановка и детская площадка рядом с EXPO.', 'A museum, a warm break and a playground near EXPO.'],
  ['Mega маңында жылы үзіліс және балалар алаңы: бір сағатқа сай екі орын.', 'Тёплая остановка и детская площадка рядом с Mega — два места на час.', 'A warm break and a playground near Mega — two stops within an hour.'],
  ['Жақын жерде ештеңе сәйкес келмеді.', 'Рядом ничего не подошло.', 'Nothing nearby matched.'],
  ['Ұшуға дейін бір қысқа аялдамаға уақыт бар.', 'До вылета есть время на одну короткую остановку.', 'There is time for one short stop before your flight.'],
  ['Жұмыс уақыты белгісіздеу орын: барар алдында тексеріңіз.', 'Часы работы этого места не подтверждены — проверьте перед визитом.', 'This place has uncertain opening hours — check before visiting.'],
  ['Бағыт сіздің уақытыңызға сыймайды.', 'Маршрут не укладывается в ваше время.', 'The route does not fit your available time.'],
  ['Мок: әуежайға 18:05-тен кешікпей шығыңыз. Бұл нақты рейс есебі емес.', 'Мок: выезжайте в аэропорт не позднее 18:05. Это не расчёт для реального рейса.', 'Mock: leave for the airport by 18:05. This is not a calculation for a real flight.'],
  ['Мок-деректер: жұмыс уақыты, бағыт және мәтіндер тек макет үшін.', 'Мок-данные: часы работы, маршрут и тексты приведены только для макета.', 'Mock data: opening hours, the route and copy are for this prototype only.'],
  ['Ұсыныстар ашық деректерге негізделеді және ескіруі мүмкін. Жұмыс уақытын барар алдында тексеріңіз. Бұл экранда мок-деректер көрсетілген.', 'Рекомендации основаны на открытых данных и могут устаревать. Проверьте часы перед визитом. На этом экране показаны мок-данные.', 'Recommendations use open data and may become outdated. Check opening hours before visiting. This screen displays mock data.'],
  ['МОК-ДЕРЕКТЕР · визуалды үлгі ғана', 'МОК-ДАННЫЕ · только визуальный пример', 'MOCK DATA · visual example only'],
  ['Уақыт: 1 сағат', 'Время: 1 час', 'Time: 1 hour'],
  ['Уақыт: 2 сағат', 'Время: 2 часа', 'Time: 2 hours'],
  ['Жаяу: 15 минутқа дейін', 'Пешком: до 15 минут', 'Walking: up to 15 minutes'],
];

const LANG_INDEX = {kk: 0, ru: 1, en: 2};
const ROW_FOR_TEXT = new Map(TEXT.flatMap(row => row.map(value => [value, row])));

function translated(value, lang) {
  if (typeof value !== 'string') return value;
  const row = ROW_FOR_TEXT.get(value);
  return row ? row[LANG_INDEX[lang] ?? 0] : value;
}

/** Return a translated copy of a local fixture without changing its factual fields. */
export function localizeAnswer(answer, lang = 'kk') {
  const localized = JSON.parse(JSON.stringify(answer));
  localized.lang = lang;
  for (const key of ['summary', 'disclaimer']) localized[key] = translated(localized[key], lang);
  localized.warnings = (localized.warnings || []).map(text => translated(text, lang));
  for (const card of localized.cards || []) {
    for (const key of ['title', 'why', 'what_to_do', 'address', 'open_note']) {
      if (key in card) card[key] = translated(card[key], lang);
    }
  }
  if (localized.data_used) {
    for (const key of ['filters', 'sources']) {
      if (Array.isArray(localized.data_used[key])) localized.data_used[key] = localized.data_used[key].map(text => translated(text, lang));
    }
  }
  return localized;
}
