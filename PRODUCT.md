# Qadam product context
<!-- impeccable:product-schema 1 -->

## Platform
Web, with desktop and mobile layouts.

## Users
Residents and visitors choosing a short walk in Astana, often using a phone outdoors.

## Product Purpose
Turn a natural-language request, location and available time into 1–3 places and a short walking plan. Users should understand why each stop fits, walking and visiting time, opening confidence and the complete route.

## Operating Context
Hackathon team of four. The approved city-guide prototype is on `codex/ux-ui-handoff`. The current task integrates its home and results into the real vanilla JavaScript application in `web/`, on a separate branch based on `main`. The integrator reviews and merges; this task does not change backend contracts or deploy the production service.

## Capabilities and Constraints
Existing API: `query`, `location`, `at`, `lang`, `overrides`, `want_route`, `use_llm`. UI and answers support ru/kk/en with the working application's language persistence. Location sent to the server is a landmark ID or H3 cell up to resolution 10. Raw GPS remains in browser memory; explicit navigation clicks may use it directly in 2GIS/Yandex links. Preserve parser defaults: unset time budget stays unset. Keep presets, modes, departure, filters, date/time, cancellation and retries. Facts and warnings come from the server; no invented photos, reviews or ratings. Mock mode must remain labelled.

## Brand Commitments
New team mark: two cobalt steps with a gold dot; QADAM wordmark. The approved city-guide composition uses a white canvas, cobalt wayfinding, yellow start action, ruled composer and continuous numbered itinerary. System fonts support Kazakh characters. Decorative footsteps follow the returned path, never a live position.

## Evidence on Hand
`web/` on `main` at 74b566d, existing production mock responses and schemas. User-approved prototype on `codex/ux-ui-handoff`. Its prior Airbnb reference lock and `DESIGN.md` supply the visual direction. Live Refero retrieval is unavailable without a subscription. Chromium automation is available; physical phones and live model keys are not part of the UI verification.

## Product Principles
Preserve working behavior and API shape. Show the whole walk before supporting details. Keep source provenance, uncertain opening hours, route estimates, crossings/stairs and budget warnings understandable. High-confidence open alone receives the definite green opening badge. Editing and map navigation must have a clear return path.

## Accessibility & Inclusion
Touch actions at least 44px, visible keyboard focus, semantic labels, reduced motion and real ru/kk/en copy. Check narrow screens and long names. Do not claim physical Safari/iOS testing from Chromium viewport emulation.
