---
name: "Qadam"
description: "A daylight city guide for a short walk in Astana."
colors:
  cobalt: "#245fc5"
  cobalt-strong: "#194b9f"
  cobalt-soft: "#edf3ff"
  start-yellow: "#f2b705"
  ink: "#202631"
  text: "#495362"
  muted: "#5c6675"
  white: "#ffffff"
  canvas: "#f7f8fa"
  line: "#dfe4eb"
  route-rule: "#dce3ec"
  composer-border: "#9eacbf"
  route-path: "#9dbbe9"
  open-bg: "#e6f4eb"
  open-ink: "#17613a"
  maybe-ink: "#166474"
  unknown-bg: "#eef0f3"
  unknown-ink: "#515b68"
  closed-bg: "#fbeae8"
  closed-ink: "#9e3029"
  ai-bg: "#f2edfb"
  ai-ink: "#6340a3"
  warning-bg: "#fff6de"
  warning-ink: "#684d08"
  focus-warm: "#b07c00"
typography:
  display:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Noto Sans\", sans-serif"
    fontSize: "clamp(52px, 4.8vw, 68px)"
    fontWeight: 750
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  hero-tagline:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Noto Sans\", sans-serif"
    fontSize: "clamp(26px, 8vw, 40px)"
    fontWeight: 750
    lineHeight: 1.12
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Noto Sans\", sans-serif"
    fontSize: "36px"
    fontWeight: 750
    lineHeight: 1.08
    letterSpacing: "-0.035em"
  title:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Noto Sans\", sans-serif"
    fontSize: "23px"
    fontWeight: 700
    lineHeight: 1.22
    letterSpacing: "-0.025em"
  body:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Noto Sans\", sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  stop-reason:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Noto Sans\", sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Noto Sans\", sans-serif"
    fontSize: "13px"
    fontWeight: 650
    lineHeight: 1.4
    letterSpacing: "normal"
  badge:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Noto Sans\", sans-serif"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "normal"
rounded:
  flat: "0px"
  badge: "5px"
  action: "6px"
  preset: "7px"
  control: "8px"
  map: "12px"
  composer: "14px"
  map-desktop: "16px"
  round: "50%"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  body: "16px"
  section: "20px"
  lg: "24px"
  xl: "32px"
  stop-gutter: "52px"
components:
  button-primary:
    backgroundColor: "{colors.start-yellow}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "13px 16px"
    height: "54px"
  button-secondary:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "10px 14px"
    height: "44px"
  button-map:
    backgroundColor: "transparent"
    textColor: "{colors.cobalt-strong}"
    rounded: "{rounded.action}"
    padding: "8px 10px"
    height: "44px"
  query-composer:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.composer}"
    padding: "0px"
  preset:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.preset}"
    padding: "10px 12px"
    height: "44px"
  badge-open:
    backgroundColor: "{colors.open-bg}"
    textColor: "{colors.open-ink}"
    typography: "{typography.badge}"
    rounded: "{rounded.badge}"
    padding: "3px 5px"
  badge-ai:
    backgroundColor: "{colors.ai-bg}"
    textColor: "{colors.ai-ink}"
    typography: "{typography.badge}"
    rounded: "{rounded.badge}"
    padding: "3px 5px"
  itinerary-stop:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.flat}"
    padding: "20px 0px 22px 52px"
  walk-overview:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.flat}"
    padding: "12px 0px"
---

# Design System: Qadam

## Overview

**Creative North Star: "A city guide, step by step"**

Qadam uses a white daylight surface, confident system typography, cobalt wayfinding and a yellow start action. Ruled conditions and an openly spaced sequence make the full walk readable before supporting detail. The existing hexagon logo keeps its cobalt body and yellow center.

This records the current designer prototype in `design/delivery/demo`, not the production application. Source precedence is `base.css` → `polish.css` → `city-home.css` → `city-results.css`, with rendered structure supplied by `preview.js`. Values were checked against computed styles at 390 × 844 and 1440 × 1000 on 19 October 2026.

**Key Characteristics:**

- White reading surfaces and graphite text.
- Cobalt navigation, yellow start, explicit semantic status.
- Numbered overview, linked stop sequence and approximate map.
- System fonts, original SVG icons and accessible touch controls.

## Colors

### Primary

- **Cobalt** carries the home city headline, navigation, focus and route statistics; **cobalt strong** carries text actions, and **cobalt soft** supports selected controls.
- **Start yellow** fills the main home search button and the first numbered stop. Its hover and active fills darken to `#e7ac00` and `#da9f00`.

### Secondary

- **Open green** is reserved for `open_status: open` with `open_confidence: high`. A medium-confidence opening uses outlined teal text on white; an unknown opening uses neutral gray. Closed uses red, with an outline-only treatment for lower confidence.
- **AI purple** is a provenance label. POI provenance uses a neutral white badge, separate from opening status.
- **Warning amber** uses a pale background and dark brown text for cautions, rather than the saturated start fill.

### Neutral

- **White** is the reading surface; **canvas** supports secondary utility surfaces. **Ink**, **text** and **muted** form the reading hierarchy. **Line** divides header/utility areas; **route rule** divides the overview and route disclosure. **Composer border** outlines the home form.
- **Route path** is the light blue map line, with a white underlay; it supports the cobalt numbered stop system without obscuring the map.

**The Color Meaning Rule.** Saturated yellow marks the start action and first stop. Green means high-confidence open only; purple identifies AI provenance.

## Typography

Use the frontmatter system font stack for display, body, labels and Leaflet. There is no downloaded display face or separate monospace identity.

- **Display:** home headline; desktop uses the display token, phones use `clamp(40px, 10.8vw, 50px)`, and 600–999px uses 56px. At widths up to 370px it is 40px; desktop viewports up to 800px high use 54px. Keep the compact line height and balanced wrap.
- **Headline:** results title; 36px on desktop and 32px below 1000px.
- **Title:** stop identity; 23px on desktop, 21px below 1000px and 20px at widths up to 370px.
- **Body:** general reading uses 16px. Stop reasons use 15px on desktop and 14px on phones; explanatory summary uses 14px. Small facts and controls use 12–13px. These are functional sizes, not a single geometric scale.
- **Input:** the home request uses 17px/1.4 on phones, 19px/1.4 on desktop, and 18px in compact desktop height. Primary search uses 17px, weight 700.

**The System Type Rule.** Use the system font stack throughout, including the map. Preserve Kazakh character coverage.

## Layout

The shared header is 72px high below 1000px and 76px on desktop. Desktop keeps the reading panel independently scrollable beside the map; the remaining viewport height is `100dvh - 76px`. Phones use normal document flow.

| Surface | Below 1000px | At least 1000px |
| --- | --- | --- |
| Home | Headline, one composer, five presets, privacy note, then a 220px map. Content padding is 28px 20px 24px; compact phones use 24px top padding. | Grid is `minmax(520px, 52%) minmax(0, 1fr)`. Composer region is capped at 680px; padding is `clamp(32px, 5.2vh, 56px) clamp(28px, 3.5vw, 58px) 32px`. The map has 20px top/bottom and 24px right margin. |
| Results | Reversible query row, title and totals, numbered overview, stop sequence, route disclosure, then a 360px map before data details. Padding is 14px 20px 28px, with 16px side padding up to 370px. | Grid is `minmax(480px, 42%) minmax(0, 1fr)`. Results are capped at 680px with 24px 32px 40px padding. Map margins are 18px 20px 20px 0. |

At 600–999px, home content is capped at 640px and its map at 584px; results inherit a 580px reading cap. Phone presets scroll horizontally; tablet and desktop home presets wrap. In the home composer, location spans the full row, then duration and time share two columns separated by a rule. They open the existing advanced options.

Results use a continuous vertical guide behind circular ranks. Desktop stop rows reserve a 52px left gutter, phones 46px. Overview buttons divide available width equally and truncate long stop labels; the full stop title remains below. The overview appears for multiple stops with a route. On phones, opening the map fills the viewport below the 72px header; Back and the selected-stop control return to the list.

## Elevation & Depth

Reading depth comes from space, rules and pale selection fills. The home composer and main itinerary rows are flat. Selected stops use a pale blue fill and an outlined rank, without lifting the entire row. Map labels, zoom controls and the phone selected-stop panel use small shadows so they remain readable over tiles. Existing result-edit form styling remains a separate utility treatment.

**The Flat Reading Rule.** The home composer and itinerary rows have no shadow. Reserve small shadows for controls floating over the map.

## Shapes

The main itinerary and overview are square and ruled. The home composer is gently curved (14px); its yellow button uses the control radius (8px), presets are tighter (7px), source/status badges tighter still (5px). Home maps use 16px corners on desktop and 12px on phones; results maps use 12px, expanding to square edges in the phone map view. Ranks and map markers are circles. Preserve the existing hexagon logo, without spreading hexagons across unrelated controls.

## Components

- **Home composer:** white, one outlined container, no shadow. The request field is borderless inside it; conditions are ruled rows. Home field focus adds a cobalt inset outline; the container border also changes to cobalt. The yellow search button is 54px high with a right arrow; generic and language controls keep at least 44px touch height.
- **Buttons and chips:** secondary actions use white with a thin neutral border. Presets use a white resting fill and cobalt border/pale blue selected or hover fill. Map text actions use cobalt strong, with a pale blue hover fill. Recompute/edit actions retain the existing cobalt button variant.
- **Language navigation:** three system-font buttons, selected state shown by a neutral gray fill and heavier text. Labels remain ҚАЗ, РУС and ENG; the prototype defaults to Kazakh.
- **Itinerary stop:** circular rank at left, identity, walking/visit facts, separate hours and source badges, then the reason. Details disclose address, confidence and sources. The map action stays beside the details control. Isolated catalogue cards retain their existing rounded card presentation; that exception is not the main results composition.
- **Route overview and map:** overview numbers link to corresponding stops. The map uses unmodified OpenStreetMap tiles and attribution. Its approximate path is a solid 7px light blue stroke over an 11px white stroke, both at 0.95 opacity; decorative SVG footsteps follow the path beneath numbered markers. It is not street navigation.
- **Focus and motion:** standard focus is a 3px cobalt outline with 3px offset; the query summary, overview and stop disclosure use warm focus for contrast. Loading footsteps alternate over 1.4s; route footsteps pulse over 3.6s. Reduced motion removes these animations and smooth movement. Direction and disclosure arrows use the authored SVG, not text glyphs.

Only original SVG interface assets and live map tiles ship in this surface; there are no generated raster visuals. Review screenshots are evidence, not product imagery.

## Do's and Don'ts

### Do:

- Do keep status words and source labels visible alongside color.
- Do preserve the different home and results panel proportions.
- Do keep the approximate-route caption and OpenStreetMap attribution visible.
- Do preserve keyboard focus, 44px action targets and reduced-motion behavior.

### Don't:

- Don't use saturated yellow or purple as an opening-status signal.
- Don't turn the main itinerary back into equal rounded card stacks.
- Don't add photos, ratings, favorites or factual claims absent from the supplied product data.
- Don't describe the approximate route or decorative footsteps as street navigation or live location.

## Localized headline and control feedback

The city and promise are separate block spans. Each locale supplies heroCity/heroTagline; screen readers receive the complete hero label. The promise is unbroken: mobile clamp(26px,8vw,40px), tablet44px, desktop clamp(36px,3.6vw,54px). The existing city display scale remains. Options use real translated summary text instead of CSS-generated copy.

A changed visible condition receives a320ms pale-yellow (#fff0b0) background acknowledgement, cancelled before a repeated update; reduced motion skips it. The existing SVG search arrow moves4px on pointer hover/keyboard focus and6px on press, over160ms; reduced motion keeps it static. No load entrance or extra wait is added.
