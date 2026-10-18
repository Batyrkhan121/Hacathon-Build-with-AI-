# Qadam: DEPLOY / T77

Ветка: `feat/deploy`. Автор: Meyrban. База: `0a486c9`.
Дата проверки условий платформ: 18.10.2026.
Внутренний ориентир готовности: 18.10 до 23:59, Астана (UTC+5). Источник —
сообщение Meyrban в рабочем чате: «мы хотим сделать сегодня до 23 59».
Это не подтверждение срока сдачи от организаторов. Официальный дедлайн уточняет
Megah напрямую у организаторов; DEPLOY не меняет сроки в CLAUDE.md и PLAN.md.

**Статус: конфигурация подготовлена; Docker build/run ещё не подтверждены.**
На машине удалённого участника Docker и WSL отсутствуют. По решению Meyrban
контейнер собирает и проверяет Батырхан на своей машине. До получения результатов
ниже T77 нельзя отмечать как проверенную в Docker. Ничего не развёрнуто.
Создание публичного сервиса, подключение биллинга и публикация кода — только после
явного согласия Megah.

## Устройство и границы работы

Один процесс FastAPI отдаёт API и статическую папку `web/`. Запрос с ориентиром
или H3-ячейкой проходит разбор, фильтрацию/ранжирование OSM-мест, генерацию
карточек и проверку фактов. Без LLM работают rules-парсер и шаблонные карточки.
Индекс numpy загружается в память; отдельная БД для демо не нужна.

В волне 3 QUALITY владеет retrieval/validate/generate, prompts и eval;
FEATURES — route/query_parser/pipeline/weather/api/llm/hours и соответствующими
тестами; POLISH — `web/**`; DEPLOY — только `Dockerfile`, `.dockerignore` и этот
документ. Контракты и общие настройки меняет интегратор. Полный список —
`docs/PLAN.md`, раздел «Волна 3». Волна 4: финальный eval и код-фриз,
слайды, видео, публикация и сдача.

В `main` эта дорожка не коммитит, не мержит и не пушит. Интегратор получает
ветку через `git fetch origin`, затем использует `/integrate origin/feat/deploy`.
На этом компьютере плагин ECC и скилл `ecc:deployment-patterns` не обнаружены.

## Что попадает в образ

- Python 3.12 slim / Debian Bookworm; зависимости из существующего `requirements.txt`.
- `app/`, `prompts/`, `web/` с текущими HTML/CSS/JS и mock JSON.
- `data/processed/poi.jsonl`, отчёт данных, `data/index/` и `data/landmarks.json`.

Снимки данных уже подготовлены: сборка не запускает pipeline и не обращается
к LLM. Пакет подготовки данных `pipeline/` приложению на этой базе не требуется
и в образ не включён.

`.dockerignore` сначала исключает всё, затем разрешает перечисленные входы.
Новые типы ресурсов/подкаталоги web нужно явно добавить в allowlist.
Git, локальные окружения, кэши, `.env` и файлы ключей не передаются в контекст.
Dockerfile использует явные COPY; секретных ARG/ENV и `COPY . .` нет.
Это защита от случайного включения локальных файлов, а не сканер ключей,
вписанных прямо в разрешённый исходный код: проверка T80/T83 остаётся обязательной.

Приложение работает с UID/GID 1000, исходники доступны на чтение, кэш —
в `/tmp/qadam-cache`. Кэш непостоянный, после пересоздания контейнера пропадает.
Один uvicorn worker слушает `0.0.0.0:$PORT`, по умолчанию 7860.
`EXPOSE 7860` — метаданные, фактический порт задаёт `PORT`.
Команда `exec` передаёт сигналы завершения Python-процессу; access log uvicorn
отключён. Не включать DEBUG для SDK и не печатать окружение.

Docker HEALTHCHECK обращается к `/api/health` на текущем `PORT`, проверяет
`status=ok` и `poi_count > 0`. Этот результат не доказывает свежесть эмбеддингов.
Платформам отдельно задаётся HTTP health check `/api/health`.

По умолчанию `LLM_ENABLED=false`, загрузка dotenv отключена. **Ключи добавляются
только в секретные настройки хостинга во время запуска**, после согласования.
Не передавать ключи через build args, команды shell, Git, Dockerfile, клиентский JS
или публичный registry. Для режима без платных API не задавать также ключ Gemini:
`LLM_ENABLED` выключает Claude, но не является общим выключателем эмбеддингов.

## Проверка Docker на машине Батырхана

Нужен запущенный Docker с Linux containers. Выполнять из отдельного checkout
`feat/deploy`, чтобы не переключать рабочую папку интегратора с незавершёнными правками.
Ветка проверки не должна содержать дополнительные незакоммиченные изменения.
Не монтировать локальные каталоги и не подключать `.env`.

PowerShell, шаг 1 — сборка и запуск. Порт специально отличается от значения по
умолчанию, чтобы проверить поддержку `PORT`. Если имя контейнера уже занято,
выбрать новое имя и использовать его во всех следующих командах.

```powershell
git branch --show-current
git rev-parse HEAD
docker version
docker build --pull -t qadam:t77 .
if ($LASTEXITCODE -ne 0) { throw 'Docker build failed' }
docker run --detach --name qadam-t77 --publish 127.0.0.1:18777:18777 --env PORT=18777 --env LLM_ENABLED=false qadam:t77
if ($LASTEXITCODE -ne 0) { throw 'Docker run failed' }
```

Шаг 2 — дождаться готовности и проверить UI, health, рекомендации без LLM:

```powershell
$base = 'http://127.0.0.1:18777'
$health = $null
for ($i = 0; $i -lt 30; $i++) {
    try {
        $candidate = Invoke-RestMethod "$base/api/health" -TimeoutSec 3
        if ($candidate.status -eq 'ok' -and $candidate.poi_count -gt 0) {
            $health = $candidate
            break
        }
    } catch { }
    Start-Sleep -Seconds 2
}
if ($null -eq $health) { throw 'Health failed or no POI loaded' }
if ($health.llm -ne 'off') { throw 'LLM must be off for this check' }
$ui = Invoke-WebRequest "$base/" -UseBasicParsing -TimeoutSec 10
if ($ui.StatusCode -ne 200 -or $ui.Content -notmatch 'Qadam') { throw 'UI failed' }

$payload = @{
    query = 'coffee near EXPO'
    location = @{ landmark_id = 'expo' }
    at = '2026-10-17T15:00:00+05:00'
    lang = 'ru'
    use_llm = $false
} | ConvertTo-Json -Depth 5
$answer = Invoke-RestMethod "$base/api/recommend" -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -TimeoutSec 30
if (@($answer.cards).Count -lt 1) { throw 'No recommendation cards' }
if ($answer.data_used.parser -ne 'rules' -or $answer.data_used.generation -ne 'template') {
    throw 'Expected deterministic recommendations'
}
$health | ConvertTo-Json -Compress
[pscustomobject]@{
    cards = @($answer.cards).Count
    parser = $answer.data_used.parser
    generation = $answer.data_used.generation
} | ConvertTo-Json -Compress
```

Историческое дневное время выбрано намеренно: оно дальше трёх часов от момента
проверки и не вызывает сетевой запрос текущей погоды. `use_llm=false` исключает
Claude и Gemini для этого запроса. Погода и карты в живом UI зависят от сети;
этот smoke не проверяет эти внешние сервисы.

Шаг 3 — права, состав и встроенный HEALTHCHECK:

```powershell
docker exec qadam-t77 python -c "import os; from pathlib import Path; assert os.getuid() == 1000; assert not list(Path('/app').rglob('.env*')); assert not Path('/app/.git').exists(); p = Path('/tmp/qadam-cache/t77-check'); p.write_text('ok'); p.unlink(); print('runtime user, cache and file boundaries: PASS')"
if ($LASTEXITCODE -ne 0) { throw 'Runtime isolation check failed' }
$dockerHealth = ''
for ($i = 0; $i -lt 30; $i++) {
    $dockerHealth = docker inspect --format '{{.State.Health.Status}}' qadam-t77
    if ($dockerHealth -eq 'healthy') { break }
    Start-Sleep -Seconds 2
}
if ($dockerHealth -ne 'healthy') { throw 'Docker HEALTHCHECK failed' }
docker stats --no-stream --format '{{.MemUsage}}' qadam-t77
docker image inspect --format '{{.Id}} {{.Size}}' qadam:t77
docker rm --force qadam-t77
```

Прислать Megah commit SHA, успешный build, image ID/размер, health JSON,
число карточек, rules/template, Docker health и потребление памяти.
Не присылать содержимое окружения или ключи. Проверить память особенно важно
из-за 512 MB у бесплатных инстансов. Эти результаты нужны до T77 READY.

## Два провайдера с бесплатными вычислительными ресурсами

Основной кандидат — **Render Free**. Резервный — **Koyeb Free instance**, однако
его бесплатный инстанс не означает бесплатную регистрацию нового аккаунта.
Ниже условия по официальным источникам, их надо повторно сверить при выборе тарифа.

| Вопрос | Render Free | Koyeb Free instance |
|---|---|---|
| Ресурсы | 512 MB RAM, 0.1 CPU; 750 бесплатных часов на workspace в месяц. [Compute](https://render.com/docs/compute-plans), [Free](https://render.com/docs/free) | Один free-инстанс на организацию: 512 MB RAM, 0.1 vCPU, 2 GB SSD; Frankfurt или Washington. [Instances](https://www.koyeb.com/docs/reference/instances) |
| Холодный старт | Сон после 15 минут без трафика; пробуждение занимает около минуты. [Free](https://render.com/docs/free) | Сон после часа без трафика, отключить его для Free нельзя. Для Deep Sleep указаны 1–5 секунд на VM, но запуск Python и загрузка данных занимают дополнительное время. [Scale to zero](https://www.koyeb.com/docs/run-and-scale/scale-to-zero) |
| Диск | Непостоянный; Free без persistent disk. [Free](https://render.com/docs/free) | Free без Volumes; снимок данных нужен в образе. [Instances](https://www.koyeb.com/docs/reference/instances) |
| Приватный код, публичное приложение | Подключается приватный GitHub, публикация репозитория не нужна. [Web services](https://render.com/docs/web-services) | Поддерживается приватный GitHub и сборка Dockerfile. [Build and deploy](https://www.koyeb.com/docs/build-and-deploy) |
| Секреты | Environment settings. Платформа также делает переменные доступными как build args: Dockerfile не объявляет и не читает секретные ARG. [Environment](https://render.com/docs/configure-environment-variables), [Docker](https://render.com/docs/docker) | Secrets с привязкой к runtime environment сервиса. [Secrets](https://www.koyeb.com/docs/reference/secrets) |
| Порт | `PORT` обычно 10000, bind на 0.0.0.0. [Web services](https://render.com/docs/web-services) | Установить exposed port и `PORT` равными 7860. [Environment variables](https://www.koyeb.com/docs/build-and-deploy/environment-variables) |
| Возможная оплата | Без карты при исчерпании оплачиваемых лимитов сервис приостанавливается; с картой возможна оплата превышений bandwidth/build minutes. [FAQ](https://render.com/docs/faq) | FAQ требует карту: hold $29 и списание за выбранный при регистрации Pro пропорционально остатку месяца; затем можно перейти на Starter. Spending cap отсутствует. Поэтому **не обещаем нулевые затраты нового аккаунта**. [Pricing FAQ](https://www.koyeb.com/docs/faqs/pricing) |

Если условие — ни одного платежа при регистрации, Koyeb не подтверждён как
подходящий вариант: Megah выбирает Render или отдельно проверяет условия уже
существующего аккаунта. Платные планы, базы данных, диски и дополнительные
инстансы не создавать автоматически.

В PLAN также упомянут HF Spaces. По текущей документации создание нового
**Docker Space требует PRO/Team**, хотя CPU Basic имеет нулевую почасовую цену.
Public Space открывает исходники; Private скрывает также приложение от жюри;
Protected оставляет код закрытым, приложение публичным и требует платного плана.
Поэтому HF не выбран как бесплатный приватный Docker-хостинг.
[Hugging Face Spaces overview](https://huggingface.co/docs/hub/en/spaces-overview).

Публичный адрес приложения сам по себе не меняет видимость GitHub. Но HTML/CSS/JS,
которые браузер получает из `web/`, доступны посетителям. Не публиковать Docker-образ
в открытом registry до разрешения публикации исходников.

## Настройки после согласия Megah

Для Render: Web Service → приватный репозиторий → согласованная проверенная ветка
или коммит → Dockerfile в корне → Free → HTTP health check `/api/health`.
Не заменять стартовую команду из образа. До завершения интеграции отключить
автоматические развёртывания; первую публичную сборку запустить только с согласия
Megah. Автодеплой не должен публиковать незавершённые правки каждой дорожки.

| Runtime настройка | Начальный режим для жюри |
|---|---|
| `PORT` | Значение платформы; на Render не переопределять без необходимости |
| `LLM_ENABLED` | `false` до отдельного включения Claude |
| `LOG_LEVEL` | `INFO` |
| `QADAM_CACHE_DIR` | `/tmp/qadam-cache` (значение образа) |
| `ANTHROPIC_API_KEY` | Отсутствует; после разрешения — только секретное поле платформы |
| `ANTHROPIC_WORKSPACE_ID` | Только если требуется типом ключа выбранного workspace |
| `CLAUDE_MODEL`, `CLAUDE_FALLBACK_MODEL` | Проверенные командой доступные модели; не менять ради деплоя наугад |
| `GEMINI_API_KEY` | Отсутствует до отдельного решения DATA/Megah о квоте и индексе |

После запуска повторить smoke по HTTPS-адресу, проверить UI на телефоне и после
простоя. Перед питчем вручную открыть приложение за несколько минут, иметь видео
и локальное демо на случай холодного старта. Не обещать постоянную готовность Free.
Откат: остановить публичный сервис или вернуть последний проверенный образ/коммит;
при нежелательных расходах также выключить LLM и отозвать его отдельный ключ.

## Ограничение расходов Claude

1. До публичного включения создать отдельный недефолтный workspace Qadam и ключ
   этого workspace. Megah задаёт небольшой согласованный месячный spend limit,
   RPM и лимиты входных/выходных токенов. У default workspace отдельные лимиты
   недоступны. Лимиты организации продолжают действовать; превышение spend limit
   блокирует запросы. [Anthropic rate and spend limits](https://platform.claude.com/docs/en/api/rate-limits).
2. Сначала подтвердить предел расходов в консоли провайдера, затем добавить ключ
   в настройки хостинга и включить `LLM_ENABLED`. Цена хостинга и API — разные счета.
3. Таймаут не ограничивает общий бюджет. Кэш уменьшает повторы, но на бесплатном
   непостоянном диске сбрасывается. Запасная модель и retries тоже расходуют токены.
4. В текущем API есть ограничение восьми одновременных LLM-запросов на процесс и
   тела запроса 16 KiB, но нет лимита запросов на посетителя и общего бюджета.
   `use_llm=false` относится только к конкретному запросу. Перед включением платного
   API нужен REQUEST FEATURES ниже; минимальный общий предохранитель — лимит провайдера.

## Результаты проверок и REQUEST

Проверено на Windows / Python 3.14.3, без Docker:

| Проверка | Результат |
|---|---|
| Полный `python -m pytest -q` | PASS, exit 0; одно предупреждение deprecation в google-genai |
| `python -m ruff check app pipeline eval scripts tests` | PASS |
| Статические проверки Dockerfile, обязательных входов, `git diff --check` | PASS |
| Повторный запуск uvicorn из отдельной временной папки с 35 разрешёнными файлами, без корневого `pipeline/` | PASS; `.env` не копировался, dotenv отключён, ключи провайдеров удалены из окружения дочернего процесса |
| HTTP `/` и `/api/health` | 200; `status=ok`, `poi_count=1222`, `llm=off` |
| HTTP `/api/recommend`, payload из инструкции выше | 200; 3 карточки, `parser=rules`, `generation=template`; 18 мс на этой машине |
| Независимое чтение Dockerfile/allowlist/инструкции | Блокирующих замечаний нет; согласован fallback порта для пустого `PORT` |

Host smoke проверяет полноту набора файлов и работу API. Это **не результат
Docker-сборки** и не прогноз скорости на бесплатном сервере. Docker build/run,
Python 3.12/Linux-зависимости, права UID 1000 и фактическая память контейнера
пока **не проверены**; это обязательная передача Батырхану по решению Meyrban.

- **REQUEST Батырхан / QA:** выполнить три шага Docker smoke выше и прислать
  результаты; до этого не помечать T77 READY. После интеграции собрать повторно
  окончательный коммит, поскольку файлы приложения меняют другие дорожки.
- **REQUEST FEATURES:** перед публичным включением Claude добавить лимиты запросов
  на посетителя/сервис; согласовать меньший максимум output tokens и число retries.
  Существующий семафор ограничивает параллельность, а не стоимость.
- **REQUEST DATA:** сверить/пересобрать индекс по текущим POI. На базе `0a486c9`
  снимок содержит 1222 POI, индекс — 1228 id; `/api/health` с `index=ready` не
  доказывает актуальность. Деплой упаковывает существующие данные без изменений.
- **REQUEST FEATURES / интегратор:** `/api/health` должен возвращать неготовность
  при нуле загруженных POI. Docker health это уже проверяет, HTTP-проверка платформы
  обычно смотрит только статус ответа.
- **REQUEST интегратор:** зафиксировать версии зависимостей/базовый digest для
  воспроизводимой финальной сборки; сейчас requirements задаёт нижние границы.
  Добавить публичную ссылку в README после согласия Megah и успешной проверки.
- **REQUEST Megah:** подтвердить хостинг, тариф/биллинг, бюджет Claude и отдельное
  разрешение публичного запуска. Подтверждение официального срока от организаторов
  передать интегратору напрямую; внутренний ориентир Meyrban не заменяет этот срок.

Файлы других дорожек не изменялись. `.env` не читался и не изменялся.
