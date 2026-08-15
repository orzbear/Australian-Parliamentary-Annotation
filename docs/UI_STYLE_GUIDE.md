# Phase 3 UI style guide

## Direction

The interface is a calm, high-information research workspace: warm off-white canvas,
white paper-like surfaces, charcoal typography, and a restrained deep-indigo accent.
It avoids government-portal blue, default framework styling, excessive nested cards,
and decorative motion.

## Tokens

| Token | Value |
|---|---|
| Canvas / surface / subtle | `#f7f7f4` / `#ffffff` / `#f0f1f5` |
| Ink / muted | `#20232a` / `#626875` |
| Primary / hover | `#3f46a5` / `#303783` |
| Success / warning / danger | `#18794e` / `#9a6700` / `#b42318` |
| Border / focus | `#d9dce5` / `#6366f1` |
| Radius | 8 px controls, 12 px surfaces, 18 px feature panels |
| Shadow | `0 8px 30px rgba(28,33,52,.08)` |
| Content widths | 72 rem page, 68 ch reading measure |
| Breakpoints | 640 px, 900 px, 1200 px |

Typography uses the local/system stack `Inter, ui-sans-serif, system-ui, Segoe UI,
sans-serif`; speech uses a readable system serif stack. Body text is at least 16 px,
speech 18–20 px with 1.75 line height. Spacing follows a 4 px base scale.

## Components

Jinja components cover buttons, cards, inputs, textareas, check/radio groups, taxonomy
choices, status badges, alerts, error summaries, progress bars, navigation,
breadcrumbs, pagination, tables, empty states, metadata, speech viewing, field
wrappers, loading indicators, and save status. Primary actions are filled indigo;
secondary are bordered; destructive actions are red and never distinguished by colour
alone.

Inputs have persistent labels, help text, error text, a 44 px minimum target, and a
high-contrast focus ring. Status badges include text/icons. Tables collapse to labelled
rows or scroll only in secondary administration surfaces. Empty states state what
happened and offer one clear next action.

## Annotation workspace

Desktop uses a minmax grid approximating 62/38. The speech pane has a restrained
metadata strip and 68-character reading column. The annotation panel and action footer
remain visible without covering content. On screens under 900 px the layout becomes
one column and sticky positioning is removed. Secondary-domain chips expose their
selected state and “n of 2.” Conditional fields retain layout rhythm when shown.

HTMX loading uses a visible textual indicator. Save results use an `aria-live=polite`
region. Keyboard shortcuts never fire inside unrelated controls except the documented
save/submit combinations. Motion is limited to 120–180 ms opacity/colour transitions
and disabled under `prefers-reduced-motion`.

## Accessibility acceptance

All pages have skip links, landmarks, one primary heading, programmatic labels,
keyboard focus visibility, meaningful button text, and accessible error summaries.
Contrast targets WCAG AA. Mobile testing covers 360 and 390 px with no primary-flow
horizontal overflow. Manual review covers keyboard-only operation, 200% zoom, focus
order, speech selection, and unsaved-change messaging.
