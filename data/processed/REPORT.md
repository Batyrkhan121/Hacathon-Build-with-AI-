# Данные Qadam: отчёт сборки

Сформирован: 2026-10-18 16:42 (UTC+5). Источник: OpenStreetMap (© участники OSM, лицензия ODbL), выгрузка через Overpass API. Файл генерирует `python -m pipeline.report`.

## Выгрузка
- Кусков выгрузки: 15 (astana_tile_0, astana_tile_1, astana_tile_2, astana_tile_3, astana_tile_4, astana_tile_5, astana_tile_6, astana_tile_7, astana_tile_8, zone_airport_nqz, zone_akorda_pyramid_hazrat, zone_baiterek_khan_shatyr, zone_expo_mega, zone_opera_nurly_zhol, fixture).
- Сырых элементов: 2851; после нормализации и дедупликации: 1228 POI.
- Добавлено вручную: 0 (места из эталона роли A, которых нет в OSM; источник — официальный сайт в `website`).
- Ориентиры, добавленные как места (объект OSM + ручная разметка): 2.
- Время выгрузки: 2026-10-18 14:04 … 2026-10-18 15:38 (UTC+5).
- Зеркала Overpass: https://maps.mail.ru/osm/tools/overpass/api/interpreter (11), https://overpass.kumi.systems/api/interpreter (4).
- Дата снимка OSM по зеркалам: maps.mail.ru: 2026-10-18; overpass.kumi.systems: 2026-06-01 … 2026-07-24. Пересекающиеся куски берутся из самого свежего снимка; `fetched_at` у записи — дата её снимка.
- Все плитки города скачаны.

## Категории
| Категория | POI | с часами работы |
|---|---:|---:|
| cafe | 317 | 35% |
| playground | 260 | 0% |
| restaurant | 173 | 38% |
| fast_food | 121 | 55% |
| bar | 104 | 59% |
| monument | 53 | 0% |
| mall | 49 | 20% |
| park | 40 | 2% |
| sport | 23 | 9% |
| attraction | 20 | 10% |
| religious | 17 | 6% |
| library | 10 | 60% |
| cinema | 9 | 78% |
| museum | 9 | 11% |
| theatre | 9 | 0% |
| gallery | 8 | 0% |
| nightlife | 3 | 33% |
| entertainment | 3 | 33% |

## Полнота полей
- `opening_hours`: 27%.
- `wheelchair` указан: 5%.
- адрес: 14%; сайт или телефон: 19%.
- тег `wikidata` в OSM: 1% (15).
- описание: нет — 82% (1001), llm_generated — 17% (210), wikidata — 1% (16), osm — 0% (1).
- название на 2+ языках: 10%.
- средний `data_confidence`: 0.39
- `rating` пуст у всех записей: настоящих рейтингов в открытых данных нет. `popularity` — документированный прокси (wiki-ссылки, полнота тегов, тип, бренд).

## Плотность (H3 r7, ≈5 км² на ячейку)
- Ячеек с данными: 46; медиана POI на ячейку: 6.5; ячеек с ≤3 POI: 17.

| Ячейка (центр) | Рядом | POI | с часами | кафе/еда |
|---|---|---:|---:|---:|
| 51.162, 71.442 | — | 156 | 32% | 121 |
| 51.175, 71.410 | — | 140 | 35% | 102 |
| 51.127, 71.406 | ТРЦ «Хан Шатыр» | 137 | 26% | 66 |
| 51.149, 71.474 | — | 128 | 30% | 74 |
| 51.151, 71.408 | Театр «Астана Опера» | 95 | 25% | 76 |
| 51.114, 71.438 | Монумент «Байтерек» | 84 | 25% | 49 |
| 51.138, 71.440 | Монумент «Байтерек» | 65 | 34% | 50 |
| 51.125, 71.472 | Мечеть «Хазрет Султан» | 58 | 24% | 36 |

Вывод для слайда «ограничения»: больше всего данных в ячейке 51.162, 71.442 (156 POI на ≈5 км²); в 17 из 46 ячеек не больше 3 POI. Часы работы известны у 27% мест — у остальных карточка пишет «нет данных о часах, проверьте перед визитом».

## Обогащение и индекс
- Ручные правки (data/manual/overrides.json): 1 мест.
- indoor только по данным: категория, явный тег OSM indoor=* (так размечены аттракционы внутри Хан Шатыра) и точка внутри контура shop=mall (добавлено этой проверкой: 0; остальные места внутри ТРЦ уже имели indoor). Неизвестен у 12 мест — не угадываем; площадки внутри контуров ТРЦ остаются outdoor по категории.
- Wikidata: описания добавлены 16 местам (из 17 с тегом).
- Описания Claude (claude-sonnet-5-5, пометка llm_generated): 210 из 213 заметных мест без описания (attraction, cinema, entertainment, gallery, library, mall, monument, museum, park, religious, theatre, viewpoint); кафе, бары, фастфуд и площадки не описываются (убрано старых: 0); отклонено проверкой на часы, цены и рейтинги: 3.
- Индекс пропущен: дневная квота Gemini исчерпана (429 RESOURCE_EXHAUSTED, бесплатный тариф: 1000 эмбеддингов в сутки); перезапустите pipeline.build_index после сброса квоты; без вектора 245 текстов.
- Векторный индекс: 1228 векторов, модель `gemini-embedding-001`, dim=768, создан 2026-10-18T16:26:04+05:00 — **устарел**: тексты POI (описания) изменились после сборки индекса; id и порядок совпадают, векторы части мест считаны по старым текстам.

## Примеры записей
```json
{"id": "osm:way/460703779", "name": "Хан Шатыр", "name_i18n": {"ru": "Хан Шатыр", "en": "Khan Shatyr"}, "category": "mall", "tags": ["indoor", "late_open", "shopping", "wheelchair"], "geo": {"lat": 51.1325089, "lon": 71.4038726}, "h3_r9": "8921538228fffff", "description": "торгово-развлекательный центр в столице Казахстана", "description_source": "wikidata", "address": "Тұран даңғылы, 37", "opening_hours": "Mo-Su,PH 10:00-23:00", "wheelchair": "yes", "indoor": true, "popularity": 0.6, "sources": ["osm", "wikidata"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:way/762780007", "name": "Abu Dhabi Plaza", "name_i18n": {}, "category": "mall", "tags": ["indoor", "shopping"], "geo": {"lat": 51.1219718, "lon": 71.4279322}, "h3_r9": "89215380483ffff", "description": "многофункциональный комплекс", "description_source": "wikidata", "address": "Сығанақ көшесі, 60/5", "opening_hours": "Mo-Su 10:00-22:00", "phone": "+7 7172 621616", "website": "https://abudhabiplaza.info/", "wheelchair": "unknown", "indoor": true, "popularity": 0.6, "sources": ["osm", "wikidata"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:node/11162304905", "name": "Атлас", "name_i18n": {"ru": "Атлас", "en": "Atlas"}, "category": "restaurant", "tags": ["coffee", "food", "indoor", "late_open", "vegetarian", "wifi"], "geo": {"lat": 51.131615, "lon": 71.4275325}, "h3_r9": "89215382353ffff", "address": "Қонаев көшесі, 29", "opening_hours": "Su-Sa 10:00-02:00", "phone": "+7 (771) 231-36-14", "website": "https://atlasrestobar.taplink.ws/", "wheelchair": "unknown", "indoor": true, "popularity": 0.15, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:way/241241552", "name": "Библиотека ЕНУ", "name_i18n": {"ru": "Библиотека ЕНУ", "kk": "ЕҰУ Кітапханасы"}, "category": "library", "tags": ["indoor", "wifi"], "geo": {"lat": 51.1604358, "lon": 71.4652674}, "h3_r9": "89215382e97ffff", "description": "Библиотека ЕНУ — библиотека в помещении с Wi-Fi.", "description_source": "llm_generated", "address": "Пушкин көшесі, 11", "opening_hours": "Mo-Fr 09:00-19:00; Sa 09:00-14:00; Su off", "website": "https://library.enu.kz", "wheelchair": "unknown", "indoor": true, "popularity": 0.15, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:node/6698615689", "name": "Restobar D51", "name_i18n": {"ru": "Restobar D51", "en": "Restobar D51"}, "category": "bar", "tags": ["indoor", "late_open", "night", "wifi"], "geo": {"lat": 51.1476868, "lon": 71.4156581}, "h3_r9": "89215382077ffff", "address": "Қорғалжын тас жолы, 2", "opening_hours": "Mo-Su 16:00-04:00", "phone": "+77775552060", "wheelchair": "unknown", "indoor": true, "popularity": 0.13, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
```
