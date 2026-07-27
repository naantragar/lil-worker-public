---
name: visualize
description: Turn an answer, comparison, algorithm, data set, architecture, or complex concept into ONE self-contained, responsive HTML page and hand it back as a file — so the user can grasp it with their eyes instead of a wall of text. USER-INVOKED ONLY (never produce an HTML page on your own initiative); run only when the user explicitly asks to see the answer as an HTML page / visualization.
user-invocable: true
---

# Visualize

Some answers are far faster to understand as a picture than as text: a flowchart, a comparison
table, a diagram, a small chart, a step-by-step algorithm, a state machine. This skill builds ONE
self-contained HTML file from the answer's real content and delivers it as a `[FILE]` the user opens.

## Hard rules
- **User-invoked only.** Build a visualization ONLY when the user explicitly asks for it (an HTML
  page / "покажи страницей" / a visual). NEVER do it proactively or "to be nice".
- **One self-contained file.** A single `.html` with inline CSS, inline SVG, and minimal inline JS.
  NO build step, NO external requests — no CDN, fonts, analytics, trackers, or network calls. It must
  render fully offline when double-clicked.
- **Substance over decoration.** Include ONLY visual forms that genuinely aid comprehension of THIS
  answer. If a paragraph is clearer than a diagram, use a paragraph. Do not add charts/animation/
  gradients "for the sake of it". The goal is faster understanding, not a design showpiece.
- **Deliver as a file.** End with a clean final message whose ONLY marker is `[FILE /abs/path.html]`
  (no tools after it, per the file-sending rule). No subdomain / publishing for now.

## Make it readable on a phone too
The user opens these on mobile. It does not need to be pixel-perfect, but it MUST be legible and
usable at ~390px wide:
- `<meta name="viewport" content="width=device-width, initial-scale=1">`, a system font stack,
  `box-sizing: border-box`, fluid widths (`max-width`, `%`, `clamp()`), and **no horizontal scroll**.
- Tables/wide diagrams: let them scroll inside their own container (`overflow-x:auto`) rather than
  blowing out the page; or restack as cards under a width breakpoint.
- **Flow / step / pipeline diagrams: build them as responsive HTML (flexbox chips), NOT a fixed
  `viewBox` SVG.** A wide fixed-width SVG scaled to a 390px phone shrinks its text to unreadable —
  the user then has to pinch-zoom (a real complaint, 2026-07-26). Instead: real HTML boxes with real
  text (`display:flex`; `flex-direction:row` on desktop, **`column` under a ~560px breakpoint**),
  arrows via CSS/Unicode (→ on desktop, ↓ when stacked), each box full-width when stacked. Text stays
  crisp and ≥ ~14px at phone width because it scales with font-size, not with a shrinking canvas.
  Only use SVG for genuinely graphical shapes (curves, geometry) — and even then keep any labels as
  HTML overlays or large enough to survive downscaling.
- Respect dark mode where cheap (`prefers-color-scheme`), and `prefers-reduced-motion` if you add any
  motion. Keep contrast readable.
- For any chart/plot, follow the `dataviz` skill's palette/axis/legend guidance (accessible colours,
  labelled axes) — but inline and dependency-free.

## Build → self-check → deliver
1. **Pick the form from the content**, not a template: flowchart / module map / comparison / report /
   explainer / timeline / small chart / algorithm walkthrough. Use the real facts of the answer.
2. **Write the single HTML file** (to `/tmp/` or the relevant working dir). Keep it lean.
3. **Self-check in a headless browser BEFORE sending** — run the headless-browser render-check
   (see the private `knowledge/browser-runtime.md`): it loads the file at desktop AND mobile widths
   and reports console/runtime errors + horizontal overflow, plus screenshots. Fix anything until it
   returns `ok: true` and `mobileOverflow: false`. Never ship a page you have not rendered.
4. **Deliver** the file via `[FILE ...]`. Optionally mention the screenshots exist if useful.

## Not this
Not a production UI (that's `frontend-design`). Not a persistent app or a served site. Not a
replacement for the text answer — it's a companion for the parts that read better as a picture.
