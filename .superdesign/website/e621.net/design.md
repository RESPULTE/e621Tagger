---
version: "superdesign-alpha"
name: "Dockyard Navy Terminal"
description: "A near-black utilitarian navy system built on dense Verdana UI type, square-cornered functional controls, and a white paper-card modal floating on an edge-to-edge flat navy field."
colors:
  background: "#001818"
  surface: "#152F56"
  surface-deep: "#020F23"
  surface-alt: "#1F3C67"
  card-paper: "#FFFFFF"
  text-primary: "#000000"
  text-on-dark: "#FFFFFF"
  text-secondary: "#B4C7D9"
  border: "#808080"
  accent-blue-mid: "#25477B"
  link: "#0000EE"
  danger: "#D82828"
  warning: "#E69500"
  highlight-pale: "#FFFFCC"
typography:
  display-lg:
    fontFamily: "Verdana"
    fontSize: "16px"
    fontWeight: 700
    lineHeight: "1.25"
  headline-md:
    fontFamily: "Verdana"
    fontSize: "18px"
    fontWeight: 700
    lineHeight: "1"
  body-md:
    fontFamily: "Verdana"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: "1.39"
  label-md:
    fontFamily: "Verdana"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: "1"
  body-roboto:
    fontFamily: "Roboto"
    fontSize: "14.4px"
    fontWeight: 400
    lineHeight: "1.4"
  accent-serif:
    fontFamily: "Times New Roman"
    fontStyle: "normal"
  utility-mono:
    fontFamily: "Arial"
spacing:
  base: "4px"
  xs: "2px"
  sm: "5px"
  md: "8px"
  lg: "14px"
  xl: "25px"
  content-max-width: "25px"
rounded:
  control: "4px"
  pill: "25px"
  flat: "4px"
components:
  navbar:
    background: "transparent"
    width: "100%"
    height: "60px"
    radius-tl: "0px"
    radius-tr: "0px"
    radius-br: "0px"
    radius-bl: "0px"
    item-count: 20
  navbar-cta:
    background: "#152F56"
    text-color: "#B4C7D9"
    radius: "0px"
    height: "24px"
  button-primary:
    background: "#25477B"
    text-color: "#FFFFFF"
    radius: "4px"
    height: "32px"
    padding: "8px"
    hover-background: "#2B538E"
  button-secondary-nav:
    background: "#25477B"
    text-color: "#FFFFFF"
    radius: "4px"
    height: "32px"
    padding: "4px 16px"
  button-outline:
    background: "transparent"
    text-color: "#808080"
    radius: "4px"
    height: "32px"
    padding: "6px 12px"
    border: "2px solid rgb(128, 128, 128)"
  card-list-item:
    background: "transparent"
    radius: "0px"
    padding: "0px"
  modal-card:
    background: "#FFFFFF"
    radius: "0px"
    padding: "16px 24px"
    border: "none"
  footer:
    background: "#152F56"
    links: 10
---
# Dockyard Navy Terminal
Source: https://e621.net/help

## Overview
This is a functional, information-dense interface system descended from early-2000s web application chrome rather than contemporary marketing design: flat navy surfaces, square 0–4px controls, and Verdana/Roboto UI type stacked tight. There is no gradient hero, no glass panel, no illustration — the identity is carried entirely by a disciplined two-tone navy palette (`#001818` field, `#152F56`/`#020F23` panel tones) and a single white rectangular modal that interrupts the dark field like a printed document. This is Swiss-adjacent in its rigor (strict rows, ruled dividers, no decorative noise) but utilitarian rather than typographic — the grid exists to organize controls and text blocks, not to impress.

## Composition
The very first screen is dominated by a modal dialog: a white card (`#FFFFFF`) centered in the upper-middle of an otherwise flat, near-black-teal viewport (`#001818`, ~95% of rendered pixels). The modal carries a bold white-on-navy title bar implied above it, a scrollable body of ruled paragraph text in black ink, two checkbox rows in a navy footer band, and two flat buttons (`Decline` ghost, `Accept` disabled-state) bottom-right and bottom-left. Below and behind this modal the page is otherwise empty — confirming the background is a deep flat void, not a gradient or imagery. Scrolling past this gate (per the following screenshots) reveals the same flat navy field persists with no texture change. The deliberate choice here is radical restraint: one interruption (the modal) on an otherwise unbroken flat field, rejecting any hero-gradient or illustrative first impression in favor of a document-first, consent-first encounter.

## Colors
`#001818` is the page background role, confirmed by the pixel field at ~95% dominance — a near-black teal-navy, not pure black. `#152F56` (72.3% of declared color area) is the primary surface/panel tone, used for the navbar, footer, and modal footer band. `#020F23` (23.4%) is a deeper structural navy for secondary panels or stacked surfaces. `#1F3C67` is a lighter accent surface reserved for hover/elevated states. `#FFFFFF` (3.5%) is rationed tightly to the one paper-card modal and primary button text — the single light interruption in the system. `#25477B` is the saturated interactive blue carrying all primary buttons. `#B4C7D9` is the muted ice-blue used as secondary text/link color on navy. `#808080` is the only border/outline color, used for ghost buttons and hairlines. Semantic hues (`#E69500` warning, `#D82828` danger, `#227D2A` success-adjacent green, `#827428` yellow) exist as declared tokens but are not visible in these screens — reserved for status badges and alerts elsewhere. Pure black `#000000` is the ink color inside the white modal only; it never appears on the navy field.

## Typography
Verdana is the system face throughout, used at fixed pixel sizes with no fluid scaling: 20px/700 for label-level emphasis, 18px/700 for headline/heading rows, 16px/700 for compact display text, and 14px/400 for body paragraphs at 1.39 line-height — tight, document-style leading suited to dense ruled text. Roboto appears as a secondary measured body face at 14.4px/400, likely from a different rendering context (modal vs. page chrome). Arial and Times New Roman are present as fallback/accent faces — Times New Roman is the closest thing to a serif signature in this system and should be reserved for any editorial or quoted text block. There is no display-hero scale; hierarchy is built entirely through weight (700 vs 400) and small size steps (14–20px), not dramatic scale jumps.

## Layout
Content is capped at an 800px max-width measure for readable text blocks — narrow by modern standards, reinforcing the document/terminal character. Spacing runs on a tight 4px-rooted scale (2, 4, 5, 8, 14, 25px) rather than an 8pt system, giving controls a compact, utilitarian density. Observed grids are irregular and content-driven rather than uniform: a 5-column grid with 6 items in rows sized [3/24/68/6 | 90/2]% — a dashboard-like asymmetric strip where one row is dominated by a single ~68%-width element; a 3-column, 3-item grid with rows [35/27/38]% suggesting three unequal stacked bands; and a 2-column, 4-item grid with tight 2px gaps in rows [97 | 97]%, i.e., two near-full-width rows of paired elements. None of these are bento or masonry in the marketing sense — they are functional data/control grids with content-determined, unequal spans. The five-card list family on the first screen renders as plain stacked full-width rows (100/100/100%) with zero padding and zero radius — a plain list layout, not a card grid.

## Components
- **Navbar**: edge-to-edge flat bar, 100% viewport width (0px inset either side), 60px tall, square all four corners (0/0/0/0 radius), transparent background, static (non-sticky implied by flat capture), holding 20 nav items — a dense utility bar, not a minimal 4–6 item marketing nav. Its CTA sits inline: `#152F56` fill, `#B4C7D9` text, 0px radius, 24px height — a flat, low-emphasis label-button rather than a pill.
- **Button — primary (hero/first-screen)**: `#25477B` fill, `#FFFFFF` text, 4px radius (slightly-rounded, not pill), 32px height, 8px padding, hover brightens to `#2B538E`. This is the main actionable control on the first screen — a small, flat, rectangular utility button, not an oversized marketing CTA.
- **Button — secondary/nav-adjacent**: same `#25477B`/`#FFFFFF`/4px-radius family, 32px height, 8px/16px padding — used beside the primary in navbar contexts; visually identical variant family, different placement.
- **Button — outline/ghost**: transparent fill, `#808080` text, 4px radius, 32px height, 6px/12px padding, 2px solid `#808080` border — used for secondary/dismissive actions (e.g., a decline-style control) beside the filled primary.
- **Modal / dialog card**: `#FFFFFF` paper surface, square corners, internal padding, holds a bold headline, a scrollable ruled-paragraph body in black ink with inline blue `#0000EE`-style links, a navy (`#152F56`) footer band containing two checkbox rows with `#808080`-bordered square checkboxes and white labels, and two flat buttons anchored left (ghost decline) and right (disabled-state solid accept). This is the single most elevated surface in the system — it sits directly on the flat navy void with no visible shadow or blur, reading as a flat cutout rather than a floating glass panel.
- **List/card family (×5, first screen)**: transparent background, 0px radius, 0px padding, each row spanning 100% width stacked vertically — a plain list layout carrying body text only, no imagery, no chips.
- **Footer**: `#152F56` background, flat, holding 10 links in a dense row/grid — structurally a scaled-down echo of the navbar's density rather than a spacious marketing footer.

## Graphics & Effects
The only verbatim effect captured is `backdrop-filter: blur(8px)`, applied to an overlay or panel layer (likely the modal's backing scrim), producing a soft frosted darkening behind the white card rather than a sharp cut — treat this as a subtle glass separation between modal and the flat navy field, not a pervasive glassmorphic theme. No gradients, meshes, noise, or patterned textures are present anywhere in the captured field; the background is flat, unbroken `#001818`. There is no imagery, illustration, or video surface on these screens — the entire visual weight is carried by flat color and ruled type blocks.

## Motion
Motion is purely functional and small-scale: keyframes named `hide-scroll`, `toast-fadeout`, `seekbar-thumb-expand`, and `seekbar-thumb-shrink` indicate transient UI feedback (scrollbar hiding, toast dismissal, and media-seekbar thumb scaling) rather than entrance choreography or scroll-linked effects. There are no hero animations, no staggered reveals, and no parallax — motion exists only to acknowledge direct interaction (a toast disappearing, a seek handle growing on grab), consistent with a dense application-chrome identity rather than a marketing site.

## Guardrails
- Never introduce rounded-pill buttons (9999px) — every control in this system is flat or 4px-radius at most.
- Never render the background as a gradient or mesh — it is a flat, near-black navy-teal field (`#001818`) with color confined to discrete panels.
- Never give the modal a drop shadow or heavy glass blur — its separation from the field is a flat white cutout with at most an 8px backdrop blur on its scrim.
- Never enlarge the primary button into a hero-scale CTA — it stays a compact 32px-tall, 4px-radius rectangle.
- Never substitute Verdana/Roboto body type with a geometric sans or add a decorative display typeface — hierarchy comes from weight and small size steps, not scale drama.
- Never pad or round the stacked list rows — they are deliberately flat, full-width, zero-radius bands.