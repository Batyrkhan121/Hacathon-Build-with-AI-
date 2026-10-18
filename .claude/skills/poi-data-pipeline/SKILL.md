---
name: poi-data-pipeline
description: "Сбор и подготовка датасета POI Астаны для Qadam. OpenStreetMap через Overpass → нормализация в app.schemas.POI → обогащение (Wikidata, описания Claude с пометкой) → эмбеддинги в data/index (Gemini, по желанию). Используй в дорожке DATA (задачи T10–T14) и при любых правках pipeline/ или data/."
argument-hint: "[ID задачи, например T10]"
---

# Пайплайн данных POI (дорожка DATA)

Аргументы: $ARGUMENTS

Цель: автоматический, воспроизводимый сбор данных. Требование кейса — «процесс сбора должен быть автоматизирован». Итог: `data/processed/poi.jsonl`, где каждая строка валидна по `app.schemas.POI`, плюс индекс `data/index/` и отчёт `data/processed/REPORT.md`.

## Файлы дорожки
- `pipeline/fetch_osm.py`: Overpass → `data/raw/osm_<area>.json`. Кэш: если файл свежее 24 ч, сеть не трогаем.
- `pipeline/normalize.py`: сырые элементы → POI (категории, теги, флаги, confidence, дедупликация).
- `pipeline/enrich.py`: описания. Порядок источников: OSM `description` → Wikidata → Claude, только для топ-N без описания.
- `pipeline/build_index.py`: эмбеддинги → `embeddings.npy`, `ids.json`, `meta.json`.
- `pipeline/run_all.py`: одна команда, идемпотентная. Флаги: `--area astana|fixture`, `--skip-fetch`, `--skip-enrich`, `--limit N`.
- `tests/test_pipeline_normalize.py`: нормализация на маленьких сырых примерах, без сети.
- `docs/DATA.md`: источники, лицензии, автоматизация, ограничения.
- Опционально `.github/workflows/refresh-data.yml` (еженедельно, вручную через workflow_dispatch). Это показывает жюри, что сбор автоматизирован.

## Overpass
- Эндпоинты перебираем по очереди при ошибке, 429 или 504:
  `https://overpass-api.de/api/interpreter`, `https://overpass.kumi.systems/api/interpreter`, `https://maps.mail.ru/osm/tools/overpass/api/interpreter`.
- POST `data=<query>`, таймаут 180 с, до 3 повторов с паузой 5→15→45 с.
- Заголовок `User-Agent: QadamHackathon/1.0 (<QADAM_CONTACT из .env>)`.
- bbox Астаны: юг 51.03, запад 71.20, север 51.27, восток 71.65.
- Для фикстура T10 бери `around:1500` вокруг EXPO (51.0894, 71.4161) и Mega Silk Way (51.0889, 71.4103).

```
[out:json][timeout:180];
(
  nwr["amenity"~"^(cafe|restaurant|fast_food|bar|pub|ice_cream|food_court|theatre|cinema|arts_centre|library|nightclub|place_of_worship|coworking_space)$"]({{bbox}});
  nwr["tourism"~"^(museum|gallery|attraction|viewpoint|artwork|theme_park|zoo|aquarium)$"]({{bbox}});
  nwr["leisure"~"^(park|garden|playground|water_park|sports_centre|ice_rink|amusement_arcade|escape_game|bowling_alley|trampoline_park|swimming_pool)$"]({{bbox}});
  nwr["shop"="mall"]({{bbox}});
  nwr["historic"~"^(monument|memorial|building|castle)$"]({{bbox}});
);
out center tags;
```
У way и relation координаты брать из `center`. Объекты без `name` выбрасываем. Исключение: `leisure=playground`, им даём имя «Детская площадка» и confidence не выше 0.4.

## Нормализация (POI)
- `id` = `osm:<type>/<id>`, например `osm:node/123`.
- Имена: `name` = `name:ru`, иначе `name`; `name_i18n` из `name:ru`, `name:kk`, `name:en`.
- Категории (Category из schemas):

  | OSM | Category |
  |---|---|
  | `cafe`, `ice_cream` | `cafe` |
  | `restaurant`, `food_court` | `restaurant` |
  | `fast_food` | `fast_food` |
  | `bar`, `pub` | `bar` |
  | `nightclub` | `nightlife` |
  | `museum` | `museum` |
  | `gallery`, `arts_centre` | `gallery` |
  | `theatre` | `theatre` |
  | `cinema` | `cinema` |
  | `library` | `library` |
  | `park`, `garden` | `park` |
  | `playground` | `playground` |
  | `viewpoint` | `viewpoint` |
  | `attraction`, `theme_park`, `zoo`, `aquarium`, `water_park` | `attraction` |
  | `artwork`, `historic=*` | `monument` |
  | `place_of_worship` | `religious` |
  | `shop=mall` | `mall` |
  | `sports_centre`, `ice_rink`, `swimming_pool` | `sport` |
  | `amusement_arcade`, `escape_game`, `bowling_alley`, `trampoline_park` | `entertainment` |
  | `coworking_space` | `other` с тегами `work_friendly`, `wifi` |

- Теги берём только из `TAG_VOCAB`. Ставим тег только по данным, не по догадке:
  - `coffee`: `cafe`, или `cuisine~coffee`.
  - `wifi`: `internet_access=wlan|yes`.
  - `power_outlets`: только `power_supply=yes`.
  - `kids`: playground/zoo/aquarium/theme_park/water_park, или `kids_area=yes`.
  - `24_7` и `late_open`: из opening_hours (закрытие ≥ 23:00).
  - `night`: bar/nightclub.
  - `view`: viewpoint, или `tower:type=observation`.
  - `free`: `fee=no`.
  - `vegetarian`: `diet:vegetarian=yes|only`.
  - `wheelchair`: при `wheelchair=yes`.
  - `art` и `history`: museum/gallery/historic.
  - `indoor` и `outdoor` выводим из категории.
- Поля: `wheelchair` из OSM, иначе `unknown`. `kids_friendly` и `indoor` по категории или тегам, иначе `None`.
- `opening_hours` как в OSM, без изменений. Его разбор делает `app/hours.py`. Ручные поправки в `data/manual/overrides.json`: часы (`opening_hours`) и описание принимаются только вместе со ссылкой `http(s)` в `source` (официальный сайт места); без ссылки поле не применяется, а в REPORT.md пишется причина.
- `data_confidence` = 0.3 + 0.15 (opening_hours) + 0.1 (website или phone) + 0.15 (описание из данных) + 0.1 (wikidata) + 0.1 (адрес) + 0.1 (больше одного языка имени), максимум 1.0. Формулу записать в docs/DATA.md.
- `popularity`: прокси, не рейтинг. Складывается так: +0.4 за wikidata или wikipedia, +0.3 × (число тегов / 30), +0.2 за attraction/museum/viewpoint, +0.1 за brand. Максимум 1.0. Поле `rating` оставляем `None`: настоящих рейтингов у нас нет, выдумывать нельзя.
- Дедупликация: одинаковое имя (casefold) и расстояние меньше 60 м → оставляем запись с большим `data_confidence`.
- `h3_r9` = `h3.latlng_to_cell(lat, lon, 9)`. `fetched_at` = дата выгрузки. `sources` начинается с `["osm"]`.

## Обогащение
- Wikidata: `wbgetentities`, до 50 id за раз, `props=descriptions|sitelinks`, `languages=ru|kk|en`. Используем, когда в OSM есть тег `wikidata`. Пишем `description_source="wikidata"`.
- Claude: только для заметных категорий (museum, gallery, theatre, cinema, library, park, viewpoint, attraction, monument, religious, entertainment) и только если описание уместно. У кафе, баров, фастфуда и площадок описаний нет; торговые центры тоже без шаблонных LLM-описаний. Лучше `null`, чем вода: модель пишет одно предложение о том, чему посвящено место и что там смотреть, только если уверена; код дополнительно обнуляет пересказ названия, категории и тегов, а также любое упоминание часов, цен, бесплатности, адресов, удобств и оценок. Вызываем `anthropic` прямо в `pipeline/enrich.py` (клиент с `default_headers=settings.anthropic_headers`; `client.messages.parse(model=settings.claude_model, ..., output_format=<Pydantic-модель>, output_config={"effort": "low"})`, без `temperature`), потому что T12 идёт параллельно с T30. В тестах сеть подменяем фейком. Промпт: одно предложение по-русски, только по переданным тегам и категории. Без цен, часов, событий и оценок; нет уверенности — `null`. Пишем `description_source="llm_generated"`, кэш в `data/cache/descriptions.json`. Ключ `ANTHROPIC_API_KEY` берём из app/config.py; без ключа шаг пропускается с пометкой в REPORT.md.

## Индекс
- Текст документа: `"{name}. {category_ru}. Теги: {tags}. {description}"`. Шаблон записать в meta.json.
- Эмбеддинги делает Gemini, только если задан `GEMINI_API_KEY` (`settings.has_embeddings`): у Anthropic API эмбеддингов нет. Без ключа индекс не строим (см. ниже).
- `google-genai`: `client.models.embed_content(model=settings.embed_model, contents=batch, config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT", output_dimensionality=settings.embed_dim))`. Батчи по 100, кэш по sha256 текста. Затем L2-нормализация, float32.
- `meta.json`: `{"model","dim","count","created_at","task_type","doc_template"}`. Порядок строк индекса совпадает с `ids.json`.
- Индекс не собирается (нет ключа или квоты)? Пайплайн не падает. Пишем предупреждение в REPORT.md, retrieval работает лексически. Квота Gemini общая на проект и суточная (1000 эмбеддингов): индекс пересобираем, когда квота свободна (сброс в полночь по тихоокеанскому времени, то есть в 12:00 по Астане) или с биллингом; эмбеддинги живых запросов (`embed_query`) делят ту же квоту, поэтому оставляем ей запас. После сборки в `meta.json` есть `docs_sha256`, а устаревших векторов 0.

## REPORT.md
Число POI по категориям; доля записей с opening_hours, wheelchair, описанием (по источникам), wikidata; 5 примеров записей; время выгрузки; использованные эндпоинты.

## Готово, когда
- `python -m pipeline.run_all` с нуля проходит меньше чем за 15 минут.
- `pytest -q` зелёный, `tests/test_contract.py` валидирует `poi.jsonl`.
- Сколько POI удалось собрать, сказано честно в REPORT.md и DATA.md (на 19.10 их 1222: больше в OSM по нашим селекторам нет). Видно, где плотность данных хорошая, а где нет: это нужно для слайда «ограничения».
- Координаты ориентиров в `data/landmarks.json` сверены с OSM (`verified: true`), добавлены «Астана Опера», «Нұрлы Жол», «Центральный парк», если они есть в OSM.

## Нельзя
2GIS, Яндекс, Google Maps scraping. Хранение данных Google Places сверх разрешённого. Выдуманные рейтинги, часы работы и «популярность» без формулы.
