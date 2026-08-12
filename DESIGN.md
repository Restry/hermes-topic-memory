# Hermes Topic Memory Panel Design

## Baseline

Dark technical console: square corners, connected tab bar, monospace telemetry, restrained orange signal, and consistent green/warn/red semantic states. No external runtime assets.

```css
--bg:#0d0d0d; --panel:#131313; --panel2:#1a1a1a;
--line:#262626; --line2:#333;
--ink:#e8e8e8; --sub:#8a8a8a; --dim:#5c5c5c;
--accent:#e55a3f; --accent-dim:#7a2e1f; --accent-soft:#1f100c;
--ok:#3fb950; --warn:#d29922; --bad:#f85149;
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
```

## Composition

- Product header first; connected navigation immediately below it; switched content follows.
- Home uses a compact KPI strip, honest 30-day activity SVG plus table, topic distribution, recent activity, and a searchable/filterable/sortable catalog.
- Panels are structural regions, not repeated decorative cards. Borders and spacing establish hierarchy.
- Orange marks selection and important signals only. Green, yellow, and red retain stable operational meaning.
- Motion is limited to 150–250ms state transitions and disabled under `prefers-reduced-motion`.

## Interaction states

Every control exposes visible hover, focus, disabled, and loading states. Loading uses explicit skeletons and text; empty states teach the next valid action; errors explain recovery and provide retry. Authentication tokens remain session-only and are never rendered or logged.

## Responsive and accessible behavior

- Meet WCAG AA contrast for text and controls.
- Preserve keyboard order and a clear focus ring.
- Provide textual summaries and table fallbacks for charts.
- At 375px, controls stack without viewport overflow; table regions may scroll horizontally.
- Use semantic headings, labels, live regions, and status text rather than color alone.

## Anti-references

No generic cream SaaS dashboard, identical rounded card grids, pill soup, decorative AI/glass/gradient treatment, gradient text, or full MindMemOS clone.
