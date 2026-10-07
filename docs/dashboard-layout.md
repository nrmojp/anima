# Dashboard layout

Help operators find status, memory, configuration, and diagnostics. Preserve
existing features, sandbox selection, and plugin-provided panels and menus.

## Language and presentation

Use the Language selector next to the clock in the header for Japanese or English.
This viewer-only preference is stored in a one-year, same-site browser cookie;
the page reloads immediately and keeps the selected sandbox URL. No admin token,
server configuration write, model call, or container restart is needed. It does
not change another viewer's language. HTML, JavaScript, API labels, and errors all
use the same allowlisted cookie preference. Unsupported/malformed cookies are ignored.
Request-local data prevents language preferences leaking across concurrent viewers.

The default dashboard language is English. Set `"locale": "ja"` in the deployed
persona's `dashboard.json` for Japanese, or `"locale": "en"` for English.
Unsupported values fall back to English when reading; the configuration editor
rejects unsupported values when saving. Existing files without a locale use English.
This setting also controls number, date, and relative-time formatting.

Core UI text is stored in `assets/messages.json`. HTML and JavaScript templates
contain `__ANIMA_I18N_<key>__` tokens, resolved and escaped by the dashboard adapter.
New UI messages must provide both languages; do not insert translated text into
stored data or translate conversation, memory, logs, filenames, or custom branding.
Extension-owned labels remain as supplied unless they match a registered UI label.
Keep persona-specific descriptions in the consumer repository, not this catalog.

The header selector reloads the browser automatically. For changes to the default
language in the admin editor, reload the browser manually; an existing viewer
cookie takes precedence. Clear that cookie to return to the configured default.
Other branding changes still refresh
normally. No model or external translation API is involved.

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
