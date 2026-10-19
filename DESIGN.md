---
name: "Qadam"
description: "A daylight city guide for a short walk in Astana."
colors:
  cobalt: "#245fc5"
  cobalt-strong: "#194b9f"
  cobalt-soft: "#edf3ff"
  start-yellow: "#f2b705"
  start-hover: "#e7ac00"
  start-active: "#da9f00"
  ink: "#202631"
  text: "#495362"
  muted: "#5c6675"
  white: "#ffffff"
  canvas: "#f7f8fa"
  line: "#dfe4eb"
  composer-border: "#9eacbf"
  route-rail: "#bdd0ee"
  route-path: "#9dbbe9"
  poi-bg: "#f1f3f6"
  open-bg: "#e2f3e8"
  open-ink: "#13633a"
  maybe-bg: "#e3f0f3"
  maybe-ink: "#0a566a"
  unknown-bg: "#eceef0"
  unknown-ink: "#434f58"
  closed-bg: "#fbe6e4"
  closed-ink: "#9f2419"
  ai-bg: "#efe9fb"
  ai-ink: "#5a3aa6"
  warning-bg: "#fff5d1"
  warning-ink: "#5a4200"
  error-bg: "#fdecea"
  error-ink: "#8f1f15"
typography:
  display:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", "Helvetica Neue", Arial, sans-serif'
    fontSize: "clamp(52px, 4.8vw, 68px)"
    fontWeight: 750
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  headline:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", "Helvetica Neue", Arial, sans-serif'
    fontSize: "36px"
    fontWeight: 750
    lineHeight: 1.1
    letterSpacing: "-0.035em"
  title:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", "Helvetica Neue", Arial, sans-serif'
    fontSize: "23px"
    fontWeight: 700
    lineHeight: 1.22
    letterSpacing: "-0.025em"
  body:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", "Helvetica Neue", Arial, sans-serif'
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.45
  stop-reason:
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.45
  condition-value:
    fontSize: "14px"
    fontWeight: 650
    lineHeight: 1.4
  badge:
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1.4
rounded:
  flat: "0px"
  badge: "5px"
  chip: "7px"
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
  button-search:
    backgroundColor: "{colors.start-yellow}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "13px 16px"
  button-search-hover:
    backgroundColor: "{colors.start-hover}"
  button-search-active:
    backgroundColor: "{colors.start-active}"
  button-primary:
    backgroundColor: "{colors.cobalt}"
    textColor: "{colors.white}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  button-directions:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
  query-field:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    padding: "17px 16px"
  language-navigation:
    backgroundColor: "{colors.white}"
    textColor: "{colors.text}"
    rounded: "{rounded.chip}"
    padding: "8px"
  filter-chip:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.chip}"
    padding: "8px 10px"
  itinerary-stop:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.flat}"
    padding: "22px 0px 22px 52px"
  badge-open:
    backgroundColor: "{colors.open-bg}"
    textColor: "{colors.open-ink}"
    typography: "{typography.badge}"
    rounded: "{rounded.badge}"
    padding: "3px 5px"
  map-stop:
    backgroundColor: "{colors.white}"
    textColor: "{colors.cobalt-strong}"
    rounded: "{rounded.round}"
    size: "34px"
---

# Design System: Qadam

## Overview

**Creative North Star: "A city guide, step by step"**

Qadam uses a white daylight surface, confident system typography, cobalt wayfinding and a yellow start action. Fine rules and an openly spaced numbered sequence keep a short walk easy to scan. The team mark is two cobalt steps with a gold dot beside the QADAM wordmark.

This captures the integrated application in `web/index.html`, with `styles.css` followed by the scoped `city-guide.css`. The approved prototype supplies the direction; the current production source supplies these tokens and components. Other prototype screens and its retired hexagon mark are outside this system capture.

**Key Characteristics:**

- White reading surfaces and graphite text.
- Cobalt wayfinding, yellow start, explicit factual status.
- Ruled controls, continuous routed stops and a quiet map.
- System fonts and SVG details with accessible touch controls.

## Colors

The palette is daylight white with cobalt guidance and a small amount of saturated yellow.

### Primary

- **Wayfinding cobalt** carries the city heading, navigation, focus and route statistics. **Strong cobalt** supports text actions; **soft cobalt** identifies selected filters and map actions.
- **Start yellow** fills the search action and first actual routed stop. Its hover and active tokens deepen the fill while retaining dark text.

### Secondary

- **Open green** means confirmed open with high confidence. **Maybe teal**, **unknown gray** and **closed red** keep uncertain and closed states distinct. These are the live application's inherited semantic colors, rather than the prototype's alternate values.
- **AI purple** identifies provenance; POI provenance uses neutral gray. **Warning amber** and **error red** pair pale fills with dark readable text.

### Neutral

- **White** is the reading surface; **canvas** supports utilities. **Ink**, **text** and **muted** set reading priority. **Line** divides content; **composer border** gives controls a stronger edge.
- **Route rail** connects routed stop numbers. **Route path** is the map stroke above a white underlay.

**The Color Meaning Rule.** Yellow marks the start action and first routed stop. Green means high-confidence open; purple identifies AI provenance.

## Typography

**Display Font / Body Font:** the frontmatter system stack, inherited by controls and map text. It preserves ru/kk/en character coverage without a downloaded typeface.

- **Display:** the desktop home headline uses the display token. Phones use `clamp(40px, 10.8vw, 50px)`; 600–999px uses 56px. The second line uses 1.12 line height and a smaller responsive size. Desktop heights up to 800px use a 54px first line.
- **Headline:** results use the headline token on desktop and 32px below 1000px.
- **Title:** stop titles use the title token on desktop, 21px below 1000px and 20px at widths up to 370px. Long names wrap.
- **Body:** general text uses the body token; stop reasons use the stop-reason token at all widths. Summaries use 14px/1.5; supporting facts and controls use 12–14px.
- **Input and labels:** request text uses 17px/1.4 on phones, 19px on desktop and 18px on short desktops. Condition values grow from their token to 16px on desktop. Small badges retain bold factual labels.

**The System Type Rule.** Use the existing system stack and preserve Kazakh character coverage.

## Layout

The spatial grammar is a white reading panel beside a map, with rules between related information. Spacing uses the recorded steps, with larger responsive gutters where the composition needs them. The integrated home/results contract is in `.impeccable/surfaces/web-index-html.md`.

At 1000px and wider, the shared header is 76px high; the remaining viewport is a grid with an independently scrolling reading panel. Home uses `minmax(520px, 52%)` plus the map; results use `minmax(480px, 42%)` plus the map. Desktop map gutters are 20–24px.

Below 1000px, content follows page flow and the compact map sits below it: 240px at home, 360px with results. Reading content is capped at 680px, or 640px for tablet home controls. Phones use 20px gutters with safe-area support. The header has a 72px minimum and may grow; map mode uses its measured height and fills the space below it. Five presets scroll horizontally on phones and wrap at tablet/desktop widths.

## Elevation & Depth

Reading surfaces, the composer and itinerary rows are flat. Fine borders, whitespace and pale selected fills provide hierarchy. Compact shadows identify controls floating above the map: caption (`0 3px 12px #20263118`), stop marker (`0 3px 8px #20263126`) and return control (`0 3px 10px #20263126`). The inherited map legend also retains a small shadow.

**The Flat Reading Rule.** Keep route content flat; use elevation where controls float over the map.

## Shapes

Controls have gently curved corners; badges and chips use smaller corners. The composer has a larger continuous outline, while map frames grow from the map radius to the desktop map radius. Itinerary rows and disclosure sections remain square and ruled. Numbered stops and map markers are circles. The steps-and-dot mark is the binding logo geometry.

## Components

### Buttons

Search is the wide yellow action at the composer base, with dark bold text, a 54px minimum height and a trailing SVG arrow. Hover/focus shifts the arrow 4px; active shifts it 6px. Other primary actions use cobalt and white; directions use white, a visible border and 44px minimum height. Cobalt text actions support editing and map navigation. Disabled actions use reduced opacity and a waiting cursor.

Visible keyboard focus is a 3px cobalt outline with a 3px offset; the textarea uses an inset outline. The existing field focus treatment also supplies a border/glow. Touch actions retain at least a 44px target.

### Chips

Filters use white outlined rounded rectangles with 44px minimum height. Selected filters use soft cobalt, a cobalt border and strong cobalt text. Presets use the same selection language with slightly larger padding. Language controls use compact rectangular hit areas and a neutral active fill.

### Cards / Containers

An itinerary stop is an open row with a numbered circular rank, a dividing rule and a connecting rail. The first actual routed row receives yellow. Alternatives and route-less recommendations retain their server ranks without the connecting rail or yellow-first treatment. The small overview uses the same ordered routed subset.

Visible stop content includes the reason, walking/visit facts, provenance, opening status, hours note and external directions. A disclosure holds what to do, address, tags, confidence, source list and website. Error/empty states use an outlined container; loading placeholders remain flat.

### Inputs / Fields

The request textarea sits in one ruled composer with location, duration and departure time. The outline changes to cobalt on focus within. Extra modes, filters and date controls expand in the same form. Keep the real labels and unset duration presentation; the surface contract records request behavior.

### Navigation

Home and results share the steps-and-dot logo, QADAM wordmark and RU/KK/EN controls. Desktop adds the product tagline. Results begin with an editable query summary. Mobile map entry focuses the visible return control; closing restores the originating control or selected stop.

### Route map and motion

Graph paths use a solid stroke; direct estimates use the same route color with a `12 8` dash pattern and explicit estimate wording. Footsteps decorate the returned path, skip stop markers and are capped at about 120. They do not represent a live position. Loading steps pulse at 1.4s; route steps at 3.6s. Reduced motion disables animation, transitions and smooth scrolling, leaving steps visible and static.

## Do's and Don'ts

### Do:

- **Do** use the current steps-and-dot mark and QADAM wordmark.
- **Do** preserve the cobalt, yellow and factual-status color meanings.
- **Do** keep route content flat, numbered and visibly connected only when routed.
- **Do** preserve visible focus, 44px touch actions and static reduced-motion states.
- **Do** use real server facts and retain uncertain opening information.

### Don't:

- **Don't** apply the prototype's retired logo or semantic color values to this integration.
- **Don't** give alternatives the route rail or first-stop yellow treatment.
- **Don't** represent direct estimates with the graph path's solid stroke.
- **Don't** use decorative footsteps as live location or progress data.
- **Don't** introduce invented photos, reviews or ratings.
