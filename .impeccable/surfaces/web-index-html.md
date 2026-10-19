---
version: 1
slug: "web-index-html"
primary_target: "web/index.html"
related_targets: ["web/city-guide.css", "web/styles.css", "web/app.js", "web/render.js", "web/map.js", "web/i18n.js"]
---

# Qadam integrated home and results

Mode: Operate. Help a resident or visitor choose a short walk, understand its actual stops and return safely between editing, results and map. This is the code-led integration of the user-approved city-guide home/results direction into the real vanilla JavaScript application. PRODUCT.md owns product facts; DESIGN.md captures the current visual system.

## Direction contract

THESIS: Start a short walk and understand its routed sequence before reading place detail; use a numbered itinerary with alternatives clearly separated.

OWN-WORLD: White daylight reading surface, system typography, cobalt headings/navigation and yellow search/first routed stop. Ruled composer, open stop rows, circular ranks and the shared two-step cobalt mark with gold dot and QADAM wordmark.

STORY: Describe the walk, set location and optional time, submit, scan the route and facts, inspect uncertainty or directions, then return to the selected stop or edit the request.

FIRST VIEWPORT: Home places a large two-line city heading above one ruled location/duration/time composer, with yellow search at its base and five presets below. Desktop uses a 52/48 panel/map split with a 520px reading minimum. Results use 42/58 with a 480px minimum: editable query, walk/places heading, truthful count/totals, optional multi-stop overview, then the stop list. Phones use page flow and a map below; the full map overlay has a visible keyboard return control. Actual text length and route size determine how many stops fit onscreen.

FORM: User-approved city guide and short itinerary, integrated code-led from the existing prototype rather than a new random composition round. Inherited direction reference: seed: bd84295207a76591 (prototype reference, not a newly rolled integration seed). Signature interaction: linked list/map stops and decorative footsteps on the returned path, with a return to the selected stop.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Reference authority and scope

- The approved prototype on `codex/ux-ui-handoff` supplies home/results composition. Its recorded Airbnb/Refero reference is `https://styles.refero.design/style/c2325884-4391-4688-85cd-e143f5107517`; live retrieval was unavailable without a subscription.
- Current `web/` source supplies factual behavior, the new team mark and exact production tokens. `styles.css` remains the base; `city-guide.css` follows it under `body.city-guide`. The page does not activate the old `v2` skin.
- This capture covers the integrated home, results, edit, loading/error/empty and mobile-map states. Other prototype screens are not current product claims. No backend or API change belongs to this visual integration.

## Behavior that constrains this surface

- Route count, totals, overview and main list use the ordered unique `route.stop_ids`; non-origin leg destinations are a compatibility fallback when IDs are absent. Other cards appear under translated alternatives, retaining server ranks and actions without a route rail or yellow-first styling. With no route, show a places heading/count and no route totals or overview.
- Keep reason, walking/visit facts, opening status/note, provenance and directions visible. Put what to do, address, tags, confidence and source list in details. Keep graph crossings/stairs, budget warnings, route timeline and data-used facts available.
- Preserve the exact request fields (`query`, `location`, `at`, `lang`, `overrides`, `want_route`, `use_llm`), parser defaults and unset duration. Preserve presets, departure, filters, date/time, cancellation, stale warning and retry without AI. Location privacy remains landmark/H3 r10; raw GPS stays in browser memory except explicit external navigation clicks.
- Interface language persists in ru/kk/en. Server facts keep the answer language until resubmission; interface language changes do not fabricate translated facts. Fixture mode stays labelled.
- Mobile map entry focuses its return control; close restores focus. The measured header height drives the overlay inset. Graph routes remain solid; direct estimates remain dashed `12 8` with estimate wording. Footsteps follow returned geometry and stay static with reduced motion.

## FINISH fulfilled

Reviewer disposition: **Ship** for this integration. The final scoped review in `/tmp/qadam-live-finish-verdict.md` resolves all three material findings: authoritative routed membership/counts with separate alternatives; visible focus on mobile map entry and return; and solid graph versus dashed direct-estimate strokes. No unresolved item remains in that scoped verdict.

Documentation: root `DESIGN.md` and `.impeccable/design.json` were extracted from the final integrated source. The sidecar uses schema 2, extends the frontmatter and provides self-contained component previews. Synthesized tonal ramps are panel previews, not new application palette tokens. Existing prototype critique and screenshots remain untouched.

Verification evidence reported by the integrator and recorded in `web/INTEGRATION.md`:

- Chromium regression: **66/66 passed**, covering 320/390/768/999/1000/1440px and ru/kk/en with intercepted responses.
- Full repository pytest and Ruff checks passed; JavaScript syntax and whitespace checks passed.
- Real local FastAPI smoke, LLM disabled: tomorrow S1 in ru/kk/en returned HTTP 200, respectively 3/2/3 cards, template generation and graph paths; desktop ru also checked at 1440px.
- Local QA captures live in `.impeccable/review/live-site/`, including route-subset and direct-estimate checks. These are test evidence, not shipped media.

Limits: Chromium viewport emulation only; no physical phone, Safari or live LLM verification. The documentation pass inspected source and existing review evidence without operating a browser or rerunning the integration tests. No generated raster asset ships, so shipping-raster provenance is not applicable. This FINISH record closes the authorized integration, not a production deployment or merge.
