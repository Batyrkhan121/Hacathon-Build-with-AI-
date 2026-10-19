# Qadam city guide — production integration

Branch: `codex/live-site-design`, based on `main` at `5155987` (the later update adds only the README screenshot).

This changes the real `web/` application. It does not import the designer fixture controller or change backend contracts. The existing prototype remains on `codex/ux-ui-handoff`.

## Design source and decisions

The approved home and results in `design/delivery/demo` on `codex/ux-ui-handoff` are the visual target. That design used the team's Airbnb reference on Refero (https://styles.refero.design/style/c2325884-4391-4688-85cd-e143f5107517), with Qadam's own cobalt wayfinding, yellow start action and continuous itinerary. Live Refero retrieval currently requires an active subscription; this implementation uses the already approved prototype and its recorded direction.

| Decision | Evidence / role |
| --- | --- |
| White canvas, confident two-line city heading, one ruled composer | Approved prototype home |
| Yellow search and first-stop rank; cobalt headings and navigation | Approved Qadam color roles |
| Full map next to the reading panel; compact map below on phones | Approved desktop/mobile composition |
| Sequential stop list, compact real totals for `route.stop_ids`, other recommendations labelled as alternatives, supporting details in disclosure | Approved results; actual API cards and route fields |
| New steps + gold dot mark, QADAM wordmark | Existing `main` header and favicon |
| Keep crossings, stairs, budget warning and precise-start navigation behavior | Working application and API contract 1.2.0 |

## What changes

- `city-guide.css` follows `styles.css`. It scopes the production integration under `body.city-guide`; old v2 styles are not activated.
- Header is shared by home and results. Home offers location, duration and time directly, plus expandable modes/filters/date controls. An unset duration remains unset in the request and is labelled “From your request”. No new default overrides the parser.
- Presentation states (`home`, `result`, `edit`, mobile map) are managed in `app.js`. Editing keeps the existing inputs; returning to results retains the submitted query and warns when conditions changed. Submitting uses the original request builder and API client.
- Results keep server `why`, opening facts, provenance and warnings visible. “What to do”, address, tags, confidence and sources are available in each stop's details. Both external directions providers, place website, map and route actions remain available. Route totals and counts use the authoritative ordered `route.stop_ids`; recommendations outside that route appear separately as nearby alternatives, retaining server ranks and facts. Without a route, the page labels cards as nearby places and shows no walking total.
- Route totals, graph crossings/stairs, `fits_budget` and data-used details remain server-driven. The full route timeline is expandable below the stops.
- Footsteps follow the returned route path. They are decorative, capped at about 120 and rebuilt on zoom/resize. Graph paths stay solid; straight estimates stay dashed to match their explicit legend and tooltip. Reduced motion keeps footsteps static.

## Preserved behavior

`api.js`, schemas, backend, retrieval, factual validation and datasets are unchanged. Requests still include `query`, `location`, `at`, `lang`, `overrides`, `want_route`, `use_llm`. Location is a landmark or H3 r10; raw GPS is kept in browser memory and used in navigation only on an explicit click. Existing presets S1–S5, departure controls, accessibility/children/group/night filters, today/tomorrow, custom time, cancellation, stale warning, retry without AI, and empty/error recovery remain.

UI language persistence and server answer language remain the application's existing behavior. Changing interface language does not invent translations for server facts; resubmit to receive a newly generated answer in that language.

## Run and review

With the normal project environment:

```sh
python -m uvicorn app.api:app --port 8000
```

Open `http://localhost:8000/`. For labelled fixtures without API or keys:

```sh
python -m http.server 5513
```

Open `http://localhost:5513/web/?mock=route`. Other existing modes: `?mock=1`, `?mock=empty`, `?mock=error`, `?mock=slow`.

Browser regression checks (Playwright with Chrome installed):

```sh
PLAYWRIGHT_MODULE=/path/to/playwright node web/tests/city-guide.cjs
```

The checks intercept API responses and cover 320/390/768/999/1000/1440px in ru/kk/en, request shape, unchanged server copy, opening details, route graph, map return/focus, stale resubmit, cancellation, language changes while loading, error retry without AI, empty state, GPS privacy, route membership/order/loops, and solid/dashed path distinction. Browser evidence is Chromium automation/emulated viewports, not physical iOS Safari testing.

Do not copy `preview.js` or fixture data into the production app. The integrator can merge this branch after the team's review; this work does not merge into `main` or deploy the real site.

## Verification in this change

- Browser integration checks: **66/66 passed**. API responses are intercepted in this suite.
- Full repository `pytest -q`: passed; `ruff check app pipeline eval scripts tests`: passed. Test environment: Python 3.13 in `/tmp/qadam-live-venv`, requirements installed with an environment-only `numpy<2.5` constraint after a stalled 2.5 wheel download; project requirements were not changed.
- Real local FastAPI on port 8013, LLM disabled: submitted S1 for tomorrow in ru/kk/en; responses were HTTP 200 with respectively 3/2/3 cards, template generation and graph paths. Also verified desktop ru at 1440px. No intercepted API responses in this smoke check.
- JavaScript syntax and `git diff --check`: passed.
- Impeccable detector ran once: 39 advisory findings (29 type-ramp, 6 palette, 3 radius, 1 stripes). These are token documentation warnings and inherited CSS; the stripes are actual pedestrian-crossing markers. No blocking findings. Visual review is separate from this mechanical scan.
- Screenshots under `.impeccable/review/live-site/` are local QA evidence and are not production media. Mock captures retain the server fixture's Russian answer text when only the interface language changes; the live API smoke verifies actual ru/kk/en answers.

Limits: no live LLM call, physical iPhone/Android or Safari verification. External OpenStreetMap tiles depend on network availability, as before.

Finish review: **ship**; all three material findings resolved.

Final integration fixes: route totals/counts/overview follow `route.stop_ids`, while alternative recommendations stay separate; keyboard focus enters the mobile map return control; graph paths and direct estimates retain distinct solid/dashed strokes.
