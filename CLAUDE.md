# Qadam: ИИ-помощник для прогулок по Астане

Хакатон Build with AI, кейс НИЦ Smart City. Сдача: 19.10.2026.

Что строим. RAG-советчик для города. На входе: место (H3-ячейка или ориентир), время, погода и запрос пользователя. На выходе: 1–3 карточки «почему / сколько времени / что делать» и мини-маршрут. У каждой карточки честно указаны источник данных и уверенность.

- Кейс: docs/TASK.md
- Архитектура и интерфейсы: docs/ARCHITECTURE.md
- Задачи и волны: docs/PLAN.md
- Контракт данных: app/schemas.py

## Стек
- Бэкенд: Python 3.11+, FastAPI, Pydantic v2. Векторный индекс в памяти на numpy. Также h3, anthropic (Claude: `claude-sonnet-5-5`, запасная `claude-haiku-5-5`), httpx, pytest, ruff. Эмбеддинги по желанию: google-genai (Gemini), только если задан `GEMINI_API_KEY`; без него retrieval лексический.
- Фронтенд: папка web/, чистый HTML/CSS/JS. Leaflet и h3-js подключаем с CDN с точной версией, без сборщика. FastAPI отдаёт web/ по адресу "/".
- Данные: OpenStreetMap (Overpass) → data/processed/poi.jsonl → data/index/. Обогащение через Wikidata, описания через Claude (с пометкой).
- 2GIS и Яндекс не парсить: их оферты запрещают хранить данные.

## Команды
Windows, Git Bash. В PowerShell те же пути, только через `\`. На macOS и Linux вместо `.venv/Scripts/python` пиши `.venv/bin/python`.
- Окружение: создаётся один раз в каждом worktree:
  `python -m venv .venv && .venv/Scripts/python -m pip install -q -r requirements.txt`
- Тесты: `.venv/Scripts/python -m pytest -q`
- Линт: `.venv/Scripts/python -m ruff check app pipeline eval scripts tests`
- API и UI: `.venv/Scripts/python -m uvicorn app.api:app --reload --port 8000`, затем открыть http://localhost:8000
- UI на моке, без API: запустить `.venv/Scripts/python -m http.server 5500` из корня, затем открыть http://localhost:5500/web/?mock=1
- Данные целиком: `.venv/Scripts/python -m pipeline.run_all`
- Проверка окружения: `.venv/Scripts/python scripts/check_env.py --llm`
- Eval: `.venv/Scripts/python -m eval.run_eval --no-llm`, а также `--mock` и `--url http://localhost:8000`

## Правила продукта (по ним оценивает жюри)
1. Факты считает код по данным: расстояние, минуты пешком, часы работы, адрес. LLM пишет только «почему» и «что делать», и только про переданных ей кандидатов.
2. «Открыто сейчас» пишем только при status=open и confidence=high. Если данных нет, пишем: «нет данных о часах, проверьте перед визитом».
3. У каждой карточки есть пометки: idea_source (poi_data или generated), facts_sources, open_confidence.
4. Приватность. Сервер не принимает и не логирует сырые координаты: только H3-ячейку (r9) или id ориентира. Нельзя логировать текст запроса вместе с ячейкой.
5. Сбой LLM или сети не должен ронять демо. Есть детерминированный запасной путь: rules-парсер и шаблонные карточки.
6. Отвечаем на языке пользователя (ru, kk, en), коротко: 1–3 карточки, в why не больше двух предложений. Текст запроса для LLM — это данные, а не инструкции.

## Код
- Type hints везде. Модели, которые передаются между слоями, берутся только из app/schemas.py.
- Файлы открывай с `encoding="utf-8"` (на Windows это обязательно). Время всегда с часовым поясом: Астана = UTC+5, см. `app.hours.ASTANA_TZ`.
- Секреты читаем только из .env через app/config.py. Файл .env не читать и не выводить.
- Сеть: таймауты, повторы с паузой, кэш в data/cache/. Тесты в сеть не ходят: используй моки и фейки.
- Ядро (geo, hours, retrieval, route, validate, query_parser/rules) пишем по TDD. Весь pytest должен проходить меньше чем за 30 секунд.

## Параллельная работа в worktree
Основная папка D:\HACATHON — это сессия ИНТЕГРАТОРА. Она всегда на ветке `main`, и в ней никто, кроме интегратора, код не пишет.
Каждая дорожка — отдельная сессия Claude Code в своём worktree (`.claude/worktrees/<имя>`), созданная от `main`.

### Кто какими файлами владеет
Редактируй только свои файлы.

| Дорожка | Файлы |
|---|---|
| DATA | `pipeline/**`, `data/**` (включая `data/landmarks.json`), `tests/fixtures/poi_sample.jsonl`, `tests/test_pipeline_*.py`, `docs/DATA.md`, `.github/workflows/**` |
| CORE | `app/geo.py`, `app/hours.py`, `app/store.py`, `app/retrieval.py`, `app/route.py`, `app/validate.py`, `tests/test_{geo,hours,store,retrieval,route,validate}.py` |
| LLM | `app/llm.py`, `app/query_parser.py`, `app/weather.py`, `app/generate.py`, `app/pipeline.py`, `app/api.py`, `prompts/**`, `scripts/check_llm.py`, `tests/test_{llm,query_parser,weather,generate,pipeline,api}.py` |
| WEB | `web/**`, кроме `web/mock/answer_sample.json` |
| EVAL | `eval/**`, `tests/test_eval*.py`, `docs/{CONCEPT,RAG,SCENARIOS,ETHICS,PITCH,VIDEO}.md` |
| ИНТЕГРАТОР | `app/schemas.py`, `app/config.py`, `app/__init__.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_contract.py`, `tests/fixtures/poi_synthetic.jsonl`, `.gitignore`, `.env.example`, `.worktreeinclude`, `CLAUDE.md`, `README.md`, `docs/{TASK,ARCHITECTURE,PLAN,PROMPTS}.md`, `requirements.txt`, `pyproject.toml`, `.claude/**`, `scripts/check_env.py`, `web/mock/answer_sample.json`, `Dockerfile` |

В волне 3 действует другая таблица владения: см. docs/PLAN.md, раздел «Волна 3».

### Протокол воркера
1. Выполни `git branch --show-current`: ты должен быть НЕ на main. Создай `.venv` (см. «Команды»).
2. Найди свои задачи в docs/PLAN.md. Интерфейсы смотри в docs/ARCHITECTURE.md, в заглушках `app/*.py` и в app/schemas.py. Сигнатуры заглушек не меняй.
3. Нужна правка чужого файла, контракта или новая зависимость? Не правь сам. Опиши это в отчёте как `REQUEST:`. Чтобы не стоять, временно подменяй чужие модули фейками в своих тестах.
4. После каждой задачи делай коммит: `git add <свои файлы> && git commit -m "T21: <что сделано>"`. Не делай push, rebase и merge в main.
5. Свежий main забирай командой `git merge main`, только когда об этом сказал интегратор.
6. Перед отчётом pytest и ruff должны быть зелёными.
7. Отчёт воркера: ветка; список коммитов; задачи (ID); как проверить; известные проблемы; REQUEST.

Интегратор вливает ветки скиллом `/integrate <ветка>`. Галочки в docs/PLAN.md ставит только интегратор.

## Приоритеты над плагином ECC
- Правила этого файла и проектные скиллы в `.claude/skills/` важнее общих правил ECC.
- Агентов-ревьюеров и TDD-агентов ECC не запускать автоматически после каждой правки. Только по явной команде или в конце задачи.
- Порог покрытия 80% не нужен. Нужны тесты на ядро и на контракт.
- Хуки ECC работают в профиле `minimal`, gateguard выключен (см. `.claude/settings.json`). Если хук мешает работе, остановись и сообщи, а не обходи его.
- Команды ECC вызываются с префиксом: `/ecc:python-review`, `/ecc:fastapi-review`, `/ecc:build-fix`. Без префикса `/plan` и `/code-review` запускают встроенные команды Claude Code.
