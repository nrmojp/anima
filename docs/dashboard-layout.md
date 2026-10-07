# Dashboard layout

Help operators find status, memory, configuration, and diagnostics. Preserve
existing features, sandbox selection, and plugin-provided panels and menus.

## References

- [Digital Agency Design System: layout](https://design.digital.go.jp/dads/foundations/layout/)
- [Digital Agency Design System: spacing](https://design.digital.go.jp/dads/foundations/spacing/)

Apply the ideas of left-hand navigation with a content area, fluid widths,
gutters, and consistent spacing to the project's own CSS. Do not incorporate
official components, code, or logos. This is not a claim of complete compliance
or accessibility certification.

## Page shell and information hierarchy

- Desktop (1024px and wider): a persistent 240px navigation index on the left
  and content on the right. The index stays at the top of the viewport and
  scrolls internally when it exceeds the viewport height. Each group can also
  be collapsed manually.
- Narrow screens: single-column content with a collapsible index above it.
  Selecting a link closes the index. Opening one group closes the others, and
  changing between width modes resets the expansion state.
- Maximum shell width: 1440px. Maximum content width: 1120px. The gutter between
  the index and content is 32px.
- Base spacing on 8/16/24/32px increments. Preserve dense data displays while
  keeping explanatory text readable.
- Use white content on a light-gray background, dark text, and blue accents for
  links and headings. Individual plugins control their own panel presentation.
- Remove oversized titles, background gradients, and decorative shadows
  unrelated to the information from the page shell.
- Provide a skip-to-content link, visible focus indicators, text wrapping, and
  appropriately sized mobile interaction targets.

Keep visual changes in a dedicated block at the end of `dashboard.css`. Preserve
existing styles inside panels. Responsive index behavior belongs in
`dashboard.js`, with regression tests in `dashboard_scope.test.cjs`.
