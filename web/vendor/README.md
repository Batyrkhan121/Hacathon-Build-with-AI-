# web/vendor — локальные копии библиотек

Демо не зависит от CDN: браузер берёт Leaflet и h3-js с нашего сервера, а не с unpkg.
Наружу из браузера уходят только запросы тайлов OpenStreetMap (`tile.openstreetmap.org`); без интернета
карта будет без подложки, но карточки, H3-ячейка и маршрут работают.

| Библиотека | Версия | Лицензия | Источник |
|---|---|---|---|
| Leaflet | 1.9.4 | BSD-2-Clause, `leaflet-1.9.4/LICENSE` | `https://unpkg.com/leaflet@1.9.4/dist/` |
| h3-js | 4.1.0 | Apache-2.0, `h3-js-4.1.0/LICENSE` и `NOTICE` | `https://unpkg.com/h3-js@4.1.0/dist/h3-js.umd.js` |

Файлы скачаны 18.10.2026 и сверены с sha256, которые публикует unpkg (`?meta`). Основные бандлы совпали
и с SRI, которые раньше стояли в `web/index.html`.

| Файл | Байт | sha256 (base64) |
|---|---:|---|
| leaflet-1.9.4/leaflet.js | 147552 | `20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=` |
| leaflet-1.9.4/leaflet.css | 14806 | `p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=` |
| leaflet-1.9.4/images/layers.png | 696 | `Hbvp0CjikvNvy6j4s6KNXokydU/CIVuaxp5M3s9RB8Y=` |
| leaflet-1.9.4/images/layers-2x.png | 1259 | `Bm2sqFDY/77wB68AsG6sABVyje4nnFHzy2xxbffELt8=` |
| leaflet-1.9.4/images/marker-icon.png | 1466 | `V0w6XMqF9BFAhbaEFZbWLwDXyJLHsD8oy/owHesdxDc=` |
| leaflet-1.9.4/images/marker-icon-2x.png | 2464 | `ABecTB7oMNOhCEEq4NKU9Vd2z+sIXGASmjmqb8SuJSg=` |
| leaflet-1.9.4/images/marker-shadow.png | 618 | `Jk9cZAM58ELdcpBiz8BMF/jqDymIK1OOOEjtjxDttNo=` |
| leaflet-1.9.4/LICENSE | 1395 | `U+jcJYYgFOQyR0HKGPvjYR4R1C72n1n4bqjFOJZH1Ms=` |
| h3-js-4.1.0/h3-js.umd.js | 168825 | `CHDZTeOFA732tj3OjEgSpC3ENWXxWuNWlK7fT5fq9Ug=` |
| h3-js-4.1.0/LICENSE | 11357 | `xx0jnfkXJvxRnG63LTGOxlggYnIysveWIZ6H3PNdCrQ=` |
| h3-js-4.1.0/NOTICE | 567 | `omW40Tj6kGS74SwenWeFcFvckaNlWyQbH9Aaeg6tZmM=` |

Проверить копию (из корня репозитория):

```bash
.venv/Scripts/python -c "import base64,hashlib,pathlib;[print(base64.b64encode(hashlib.sha256(p.read_bytes()).digest()).decode(), p.as_posix()) for p in sorted(pathlib.Path('web/vendor').rglob('*')) if p.is_file() and p.suffix != '.md' and p.name != '.gitattributes']"
```

`integrity` в `index.html` для своих файлов не ставим: если байты разойдутся (например, из-за перевода
строк), SRI заблокирует скрипт и карта сломается целиком; сверка — по таблице выше. Обновление версии:
скачать новые файлы с unpkg в новую папку `<имя>-<версия>/`, сверить с `?meta`, поменять пути в `index.html`.
