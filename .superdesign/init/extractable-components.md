# Extractable components

## Layout components

### AppShell
- Source: `frontend/src/App.tsx`
- Category: `layout`
- Description: Shared header, auth boundary, route host, optional demo banner, and footer.
- Extractable props: none currently; auth and route state are internal.
- Hardcoded: text wordmark, Projects link, auth/demo/footer copy, classes, shell styles.

### SiteHeader
- Source: `frontend/src/App.tsx`
- Category: `layout`
- Description: Compact top bar with text wordmark, Projects link, and conditional sign-out.
- Extractable props: session and passwordless state (currently internal).
- Hardcoded: labels, wordmark text, CSS classes.

### SiteFooter
- Source: `frontend/src/App.tsx`
- Category: `layout`
- Description: Shared compact footer.
- Extractable props: none.
- Hardcoded: “Hydra · Private tagging workspace” and `footer` class.

## Basic components

### QueryInput
- Source: `frontend/src/components/QueryInput.tsx`
- Category: `basic`
- Description: e621 query field with accessible tag autocomplete; used on home and project pages.
- Extractable props: `value`, `onChange`, `label`, `placeholder`.
- Hardcoded: suggestion behavior/markup, category/count display, CSS classes.

### ReviewButtons
- Source: `frontend/src/components/Reviews.tsx`
- Category: `basic`
- Description: Positive, negative, unsure, save status, undo, and retry controls used in gallery focus and post detail.
- Extractable props: `project`, `post`, `onSaved`.
- Hardcoded: state labels, keyboard hints, strings, classes.

### FavoriteButton
- Source: `frontend/src/components/FavoriteButton.tsx`
- Category: `basic`
- Description: Collection-save toggle on gallery cards and post detail.
- Extractable props: `project`, `post`, `compact`.
- Hardcoded: heart glyph, labels, classes.

### TagSidebar
- Source: `frontend/src/components/TagSidebar.tsx`
- Category: `basic`
- Description: Category-colored linked e621 tags on gallery and post detail.
- Extractable props: `tags`, `hidden`.
- Hardcoded: category order/labels, destination pattern, classes.

### Thumbnail
- Source: `frontend/src/components/Thumbnail.tsx`
- Category: `basic`
- Description: Candidate preview/loading state used by the virtualized gallery.
- Extractable props: `id`, `available`, `onLoaded`.
- Hardcoded: URL behavior, placeholder copy, classes.

### EmbeddingAssist
- Source: `frontend/src/components/EmbeddingAssist.tsx`
- Category: `basic`
- Description: Optional similar-post evidence/import panel on post detail.
- Extractable props: `project`, `post`, `hideTags`.
- Hardcoded: help/provenance copy, labels, hierarchy, classes.
