# Данные Qadam: отчёт сборки

Сформирован: 2026-10-18 20:26 (UTC+5). Источник: OpenStreetMap (© участники OSM, лицензия ODbL), выгрузка через Overpass API. Файл генерирует `python -m pipeline.report`.

## Выгрузка
- Кусков выгрузки: 15 (astana_tile_0, astana_tile_1, astana_tile_2, astana_tile_3, astana_tile_4, astana_tile_5, astana_tile_6, astana_tile_7, astana_tile_8, zone_airport_nqz, zone_akorda_pyramid_hazrat, zone_baiterek_khan_shatyr, zone_expo_mega, zone_opera_nurly_zhol, fixture).
- Сырых элементов: 2851; после нормализации и дедупликации: 1222 POI.
- Добавлено вручную: 0 (места из эталона роли A, которых нет в OSM; источник — официальный сайт в `website`).
- Ориентиры, добавленные как места (объект OSM + ручная разметка): 4.
- Время выгрузки: 2026-10-18 14:04 … 2026-10-18 15:38 (UTC+5).
- Возраст данных: самый старый кусок fixture скачан 2026-10-18 14:04 (6 ч назад); при сбое выгрузки берутся файлы с диска.
- Контуры ТРЦ: скачаны 2026-10-18 16:40 (3 ч назад).
- Зеркала Overpass: https://maps.mail.ru/osm/tools/overpass/api/interpreter (11), https://overpass.kumi.systems/api/interpreter (4).
- Дата снимка OSM по зеркалам: maps.mail.ru: 2026-10-18; overpass.kumi.systems: 2026-06-01 … 2026-07-24. Пересекающиеся куски берутся из самого свежего снимка; `fetched_at` у записи — дата её снимка.
- Все плитки города скачаны.

## Категории
| Категория | POI | с часами работы |
|---|---:|---:|
| cafe | 317 | 35% |
| playground | 260 | 0% |
| restaurant | 171 | 39% |
| fast_food | 122 | 55% |
| bar | 104 | 59% |
| monument | 53 | 0% |
| mall | 48 | 21% |
| park | 38 | 3% |
| sport | 23 | 9% |
| attraction | 18 | 11% |
| religious | 17 | 6% |
| library | 10 | 60% |
| cinema | 9 | 78% |
| museum | 9 | 22% |
| theatre | 9 | 0% |
| gallery | 7 | 0% |
| entertainment | 4 | 25% |
| nightlife | 3 | 33% |

## Полнота полей
- `opening_hours`: 28%.
- `wheelchair` указан: 5%.
- адрес: 14%; сайт или телефон: 19%.
- тег `wikidata` в OSM: 1% (15).
- описание: нет — 97% (1182), llm_generated — 3% (37), osm — 0% (1), wikidata — 0% (1), manual — 0% (1).
- название на 2+ языках: 10%.
- средний `data_confidence`: 0.39
- `rating` пуст у всех записей: настоящих рейтингов в открытых данных нет. `popularity` — документированный прокси (wiki-ссылки, полнота тегов, тип, бренд).

## Плотность (H3 r7, ≈5 км² на ячейку)
- Ячеек с данными: 46; медиана POI на ячейку: 6.5; ячеек с ≤3 POI: 17.

| Ячейка (центр) | Рядом | POI | с часами | кафе/еда |
|---|---|---:|---:|---:|
| 51.162, 71.442 | — | 156 | 32% | 121 |
| 51.175, 71.410 | — | 140 | 35% | 102 |
| 51.127, 71.406 | ТРЦ «Хан Шатыр» | 135 | 26% | 66 |
| 51.149, 71.474 | — | 128 | 30% | 75 |
| 51.151, 71.408 | Театр «Астана Опера» | 95 | 25% | 76 |
| 51.114, 71.438 | Монумент «Байтерек» | 84 | 25% | 49 |
| 51.138, 71.440 | Монумент «Байтерек» | 64 | 34% | 49 |
| 51.125, 71.472 | Мечеть «Хазрет Султан» | 57 | 25% | 35 |

Вывод для слайда «ограничения»: больше всего данных в ячейке 51.162, 71.442 (156 POI на ≈5 км²); в 17 из 46 ячеек не больше 3 POI. Часы работы известны у 28% мест — у остальных карточка пишет «нет данных о часах, проверьте перед визитом».

## Обогащение и индекс
- Нормализация не взяла: без имени (кроме площадок) 258, без координат 0, вне наших категорий 0; склеено дублей 90; нестандартный wheelchair → unknown: нет.
- Устаревшие объекты не взяты: 24 (есть только в старом снимке зеркала, а в более свежем полном куске на той же территории их уже нет: закрыты, удалены или переразмечены в OSM). Из них подходили под наши категории (до склейки дублей) 17: Детская площадка (osm:node/2640898898); Детская площадка (osm:node/2640898899); Детская площадка (osm:node/2640898903); Детская площадка (osm:node/2640898904); Детская площадка (osm:node/2640898905); Детская площадка (osm:node/2640898906); Детская площадка (osm:node/2640898910); Astana Music Hall (osm:node/4929123040); Узбекистан (osm:node/5936638687); Детская площадка (osm:node/13308051627); Детская площадка (osm:node/13308051628); Детская площадка (osm:node/13308087158); Детская площадка (osm:node/13308087159); GreenLine (osm:way/1453849422); Круглая площадь (osm:relation/3466339); Арка входа в Ботанический сад (osm:node/11783594720); 25-ти летие Независемости (osm:node/13041707198).
- Ручные правки (data/manual/overrides.json): 4 мест.
- indoor только по данным: категория, явный тег OSM indoor=* (так размечены аттракционы внутри Хан Шатыра) и точка внутри контура shop=mall (добавлено этой проверкой: 2; остальные места внутри ТРЦ уже имели indoor). Неизвестен у 10 мест — не угадываем; площадки внутри контуров ТРЦ остаются outdoor по категории.
- Wikidata: описания добавлены 1 местам из 16 с тегом; короткие и пустые по смыслу не взяты: 14.
- Описания Claude (claude-sonnet-5-5, пометка llm_generated): 37 из 171 заметных мест без описания (attraction, cinema, entertainment, gallery, library, monument, museum, park, religious, theatre, viewpoint); кафе, бары, фастфуд, площадки и ТРЦ не описываются; модель не уверена (null): 134; отклонено проверкой (часы, цены, удобства, адреса, пересказ названия и тегов): 0. Новых запросов не было (нет ANTHROPIC_API_KEY или LLM_ENABLED=false): только из кэша.
- Векторный индекс: 1222 векторов, модель `google/gemini-embedding-001`, dim=768, создан 2026-10-18T20:26:20+05:00.

## Примеры записей
```json
{"id": "osm:way/454857501", "name": "Alem.ai (бывш. Нур Алем)", "name_i18n": {"ru": "Alem.ai (бывш. Нур Алем)", "kk": "Alem.ai (бұрынғы Нұр Әлем)", "en": "Alem.ai (formerly Nur Alem)"}, "category": "museum", "tags": ["architecture", "indoor", "interactive", "kids", "photo_spot"], "geo": {"lat": 51.08925, "lon": 71.41619}, "h3_r9": "892153806dbffff", "description": "Бывший шар Нур Алем, теперь международный центр ИИ Alem.ai. На первом этаже — Музей Сингулярности: иммерсивная выставка и интерактивные зоны. Билет 2 500 ₸, дети до 7 лет бесплатно.", "description_source": "manual", "opening_hours": "Tu-Su 10:00-18:30; Mo off", "website": "https://alem.ai/", "wheelchair": "unknown", "kids_friendly": true, "indoor": true, "popularity": 0.61, "sources": ["osm", "manual", "wikidata"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:way/460703779", "name": "Хан Шатыр", "name_i18n": {"ru": "Хан Шатыр", "en": "Khan Shatyr"}, "category": "mall", "tags": ["indoor", "late_open", "shopping", "wheelchair"], "geo": {"lat": 51.1325089, "lon": 71.4038726}, "h3_r9": "8921538228fffff", "address": "Тұран даңғылы, 37", "opening_hours": "Mo-Su,PH 10:00-23:00", "wheelchair": "yes", "indoor": true, "popularity": 0.6, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:way/762780007", "name": "Abu Dhabi Plaza", "name_i18n": {}, "category": "mall", "tags": ["indoor", "shopping"], "geo": {"lat": 51.1219718, "lon": 71.4279322}, "h3_r9": "89215380483ffff", "address": "Сығанақ көшесі, 60/5", "opening_hours": "Mo-Su 10:00-22:00", "phone": "+7 7172 621616", "website": "https://abudhabiplaza.info/", "wheelchair": "unknown", "indoor": true, "popularity": 0.6, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:node/11162304905", "name": "Атлас", "name_i18n": {"ru": "Атлас", "en": "Atlas"}, "category": "restaurant", "tags": ["coffee", "food", "indoor", "late_open", "vegetarian", "wifi"], "geo": {"lat": 51.131615, "lon": 71.4275325}, "h3_r9": "89215382353ffff", "address": "Қонаев көшесі, 29", "opening_hours": "Su-Sa 10:00-02:00", "phone": "+7 (771) 231-36-14", "website": "https://atlasrestobar.taplink.ws/", "wheelchair": "unknown", "indoor": true, "popularity": 0.15, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
{"id": "osm:way/241241552", "name": "Библиотека ЕНУ", "name_i18n": {"ru": "Библиотека ЕНУ", "kk": "ЕҰУ Кітапханасы"}, "category": "library", "tags": ["indoor", "wifi"], "geo": {"lat": 51.1604358, "lon": 71.4652674}, "h3_r9": "89215382e97ffff", "address": "Пушкин көшесі, 11", "opening_hours": "Mo-Fr 09:00-19:00; Sa 09:00-14:00; Su off", "website": "https://library.enu.kz", "wheelchair": "unknown", "indoor": true, "popularity": 0.15, "sources": ["osm"], "data_confidence": 0.75, "fetched_at": "2026-10-18"}
```
