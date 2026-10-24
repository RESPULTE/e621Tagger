# Theme and design tokens

## Compact token summary

- Framework: React + Vite, custom vanilla CSS; no Tailwind config or component library.
- Palette (`:root`): page `#012e56`; header/footer `#002442`; sidebar `#062f51`; card `#073152`; panel `#0b3b63`; input `#082b49`; media `#001e37`; border `#366185`; muted `#9eb7cb`; link/accent `#b4d9f8`; primary gold `#e6b857` (hover `#f2ce82`); positive `#8ed8a1`; negative `#f3a5a5`; unsure `#e8cc83`.
- Font: Arial, Helvetica, sans-serif. Base 16px; h1 26px (22px mobile), h2 20px, h3 15px, labels 13px, small/status 10–12px.
- Spacing: no formal scale; common values 4–36px (4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 36). Gallery gap 12px desktop / 8px mobile.
- Radius: mostly 2–3px; pill badges 20px. Shadow: suggestion popup `0 8px 20px #00152580`.
- Breakpoints: mobile `max-width: 760px`; wide desktop `min-width: 1500px`. Dark-only (`color-scheme: dark`); gold focus outline and reduced-motion support.

## Raw stylesheet source

### `frontend/src/style.css`

```css
:root {
  font-family: Arial, Helvetica, sans-serif;
  color: #d6e4ef;
  background: #012e56;
  font-synthesis: none;
  color-scheme: dark;
  --panel: #0b3b63;
  --line: #366185;
  --muted: #9eb7cb;
  --accent: #b4d9f8;
  --positive: #8ed8a1;
  --negative: #f3a5a5;
  --unsure: #e8cc83;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
}
a {
  color: #b4d9f8;
  text-decoration: none;
}
a:hover {
  text-decoration: underline;
}
button,
input,
select,
textarea {
  font: inherit;
}
button {
  background: #174b77;
  color: #e2edf5;
  border: 1px solid #50769a;
  border-radius: 3px;
  padding: 8px 12px;
  cursor: pointer;
}
button:hover:not(:disabled) {
  background: #27638f;
}
button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
button:focus-visible,
a:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible {
  outline: 2px solid #f5cf77;
  outline-offset: 3px;
}
.primary {
  background: #e6b857;
  border-color: #e6b857;
  color: #142b40;
  font-weight: bold;
}
.primary:hover:not(:disabled) {
  background: #f2ce82;
}
input,
select,
textarea {
  display: block;
  width: 100%;
  background: #082b49;
  border: 1px solid #507393;
  color: #edf6fc;
  border-radius: 3px;
  padding: 9px 10px;
  margin-top: 6px;
  min-width: 0;
}
textarea {
  min-height: 76px;
  resize: vertical;
}
label {
  font-size: 13px;
  color: #c5d9e8;
}
.check {
  display: flex;
  gap: 6px;
  align-items: center;
}
.check input {
  width: auto;
  margin: 0;
}
h1,
h2,
h3,
p {
  margin-top: 0;
}
h1 {
  font-size: 26px;
  font-weight: 600;
  margin-bottom: 10px;
}
h2 {
  font-size: 20px;
}
h3 {
  font-size: 15px;
}
small,
.muted {
  color: var(--muted);
}
kbd {
  font: 11px monospace;
  opacity: 0.75;
}
.site-header {
  height: 58px;
  background: #002442;
  border-bottom: 2px solid #507393;
  display: flex;
  align-items: center;
  padding: 8px 20px;
  gap: 28px;
}
.wordmark {
  font-size: 27px;
  font-weight: bold;
  color: #f5c960;
  letter-spacing: -1px;
  line-height: 1;
}
.wordmark:hover {
  text-decoration: none;
}
.wordmark span {
  display: block;
  font-size: 11px;
  text-transform: uppercase;
  color: #c3d6e6;
  letter-spacing: 2px;
  margin-top: 6px;
}
.site-header nav {
  display: flex;
  align-items: center;
  gap: 24px;
  font-size: 14px;
}
.site-header nav span {
  color: var(--muted);
  font-size: 12px;
}
.site-header > button {
  margin-left: auto;
  font-size: 13px;
}
.workspace {
  display: grid;
  grid-template-columns: 210px minmax(0, 1fr);
  min-height: calc(100vh - 140px);
}
.sidebar {
  background: #062f51;
  padding: 18px 14px;
  border-right: 1px solid #2c5577;
  overflow-wrap: anywhere;
}
.sidebar h2 {
  margin: 20px 0 8px;
}
.sidebar p {
  font-size: 13px;
  line-height: 1.6;
}
.main-content {
  padding: 18px 20px;
  min-width: 0;
}
.page-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 8px;
}
.page-heading h1 {
  margin: 0;
}
.page-heading h1 small {
  font-size: 14px;
  font-weight: normal;
}
.eyebrow {
  font-size: 10px;
  letter-spacing: 1.6px;
  color: #a9c3d7;
  margin-bottom: 8px;
}
.pill {
  font-size: 11px;
  border: 1px solid var(--line);
  border-radius: 20px;
  padding: 6px 10px;
  white-space: nowrap;
}
.tabs {
  display: flex;
  border-bottom: 1px solid var(--line);
  gap: 4px;
  margin: 18px 0;
}
.tabs button {
  border-bottom: 0;
  border-radius: 4px 4px 0 0;
  padding: 12px 18px;
}
.tabs button.active {
  background: #35668b;
}
.search-bar {
  display: flex;
  align-items: end;
  gap: 14px;
  flex-wrap: wrap;
}
.search-input {
  flex: 1;
  min-width: 220px;
}
.search-bar > label:not(.search-input) {
  max-width: 180px;
}
.search-bar .check {
  padding: 10px 0;
}
.tag-results {
  margin-top: 24px;
  max-width: 1000px;
}
.tag-result {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
  border-bottom: 1px solid #2b5778;
  padding: 12px;
}
.tag-result:nth-child(odd) {
  background: #0a3559;
}
.tag-result h3 {
  margin-bottom: 6px;
}
.tag-result span {
  font-size: 12px;
}
.project-link {
  display: block;
  border-bottom: 1px solid #315572;
  padding: 13px 0;
}
.project-link span {
  display: block;
  color: var(--muted);
  font-size: 11px;
  margin-top: 6px;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  padding: 18px;
}
.custom-form {
  max-width: 650px;
  display: grid;
  gap: 18px;
}
.empty-state {
  padding: 60px 20px;
  color: #b7ccdc;
  text-align: center;
}
.empty-state h2 {
  font-weight: normal;
}
.login-card {
  width: min(460px, calc(100% - 32px));
  margin: 10vh auto;
  background: var(--panel);
  padding: 32px;
  border: 1px solid var(--line);
  border-top: 3px solid #e6b857;
}
.login-card form {
  display: grid;
  gap: 20px;
}
.login-card p {
  line-height: 1.6;
  color: #b7ccdc;
}
.centered {
  text-align: center;
  padding: 50px;
}
.demo-banner {
  background: #5c4a1e;
  color: #ffe5a6;
  padding: 8px 20px;
  text-align: center;
  font-size: 12px;
}
.count-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin: 14px 0;
}
.count-grid button {
  text-align: left;
  font-size: 11px;
  padding: 10px;
}
.count-grid strong {
  display: block;
  font-size: 19px;
  margin-bottom: 5px;
}
.positive {
  color: var(--positive);
}
.negative {
  color: var(--negative);
}
.unsure {
  color: var(--unsure);
}
.unreviewed {
  color: #b1c5d8;
}
.filters label {
  display: block;
  margin-top: 12px;
}
.filters summary,
.sidebar-tags summary,
.detail-tags summary {
  cursor: pointer;
  font-size: 14px;
  font-weight: bold;
  margin: 16px 0;
}
.sidebar > .check {
  margin-top: 22px;
}
.tag-list section {
  margin: 22px 0;
}
.tag-list h3 {
  text-transform: capitalize;
  font-size: 13px;
  border-bottom: 1px solid #315572;
  padding-bottom: 5px;
  margin-bottom: 8px;
}
.tag-list a {
  display: block;
  font-size: 12px;
  margin: 7px 0;
}
.tag-artist {
  color: #f5c35b;
}
.tag-copyright {
  color: #d49df4;
}
.tag-character {
  color: #8bd478;
}
.tag-species {
  color: #edc786;
}
.tag-general {
  color: #b4d9f8;
}
.tag-meta {
  color: #bbb;
}
.tag-lore {
  color: #85cdd0;
}
.tag-invalid {
  color: #f78a8a;
}
.tag-contributor {
  color: #e8a46d;
}
.gallery-content {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.import-status {
  font-size: 12px;
  color: #b2c9da;
  padding-bottom: 7px;
  border-bottom: 1px solid #2b5576;
}
.additional-search {
  display: flex;
  gap: 8px;
}
.additional-search input {
  margin: 0;
  max-width: 600px;
}
.selection-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-height: 32px;
  font-size: 12px;
}
.selection-bar button {
  font-size: 11px;
  padding: 5px 8px;
}
.gallery-scroll {
  height: calc(100dvh - 240px);
  min-height: 280px;
  overflow: auto;
  scrollbar-gutter: stable;
}
.gallery-row {
  display: grid;
  gap: 12px;
  padding: 3px 4px 12px;
}
.post-card {
  background: #073152;
  border: 1px solid #325877;
  min-width: 0;
  display: flex;
  flex-direction: column;
  border-radius: 2px;
  overflow: hidden;
  color: #d6e4ef;
}
.post-card.focused {
  outline: 2px solid #b8d3e8;
  outline-offset: -1px;
}
.post-card.positive {
  border-bottom: 3px solid var(--positive);
}
.post-card.negative {
  border-bottom: 3px solid var(--negative);
}
.post-card.unsure {
  border-bottom: 3px solid var(--unsure);
}
.card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 10px;
  color: var(--muted);
  padding: 6px;
}
.card-top .check {
  font-size: 11px;
}
.thumbnail-link {
  height: 154px;
  width: 100%;
  background: #092b46;
  border: 0;
  border-radius: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  position: relative;
}
.thumbnail-link:hover:not(:disabled) {
  background: #103e60;
}
.thumbnail-link img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}
.thumbnail-placeholder {
  font-size: 11px;
  color: #94aec3;
}
.video-badge {
  position: absolute;
  bottom: 5px;
  left: 5px;
  padding: 3px 6px;
  background: #001a30dd;
  color: #fff;
  font-size: 11px;
}
.card-meta {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  padding: 7px 6px;
  gap: 4px;
}
.quick-labels {
  display: flex;
  border-top: 1px solid #2c5271;
}
.quick-labels button {
  flex: 1;
  border: 0;
  border-radius: 0;
  padding: 5px;
  font-size: 17px;
  background: #0d3a5c;
}
.pagination {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 18px;
  font-size: 12px;
}
.pagination button {
  font-size: 12px;
}
.focused-review {
  display: flex;
  align-items: center;
  gap: 20px;
  border-top: 1px solid var(--line);
  padding-top: 12px;
  font-size: 12px;
}
.review-block {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.review-buttons {
  display: flex;
  gap: 6px;
}
.review-buttons .chosen {
  outline: 2px solid currentColor;
  outline-offset: -2px;
  background: #234f70;
}
.save-state {
  font-size: 11px;
  color: #b7ccdb;
}
.quiet {
  background: transparent;
  font-size: 12px;
  padding: 5px;
}
.error {
  color: #ffb5a5;
  background: #5d252a77;
  padding: 12px;
  overflow-wrap: anywhere;
}
.save-errors {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  background: #622c2e;
  color: #fff;
  padding: 14px 24px;
  z-index: 5;
  font-size: 13px;
  max-height: 25vh;
  overflow: auto;
}
.save-errors div {
  margin-top: 8px;
}
.save-errors button {
  padding: 4px 8px;
  margin-left: 8px;
  font-size: 12px;
}
.post-navigation {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  font-size: 13px;
}
.post-content .page-heading {
  margin-bottom: 18px;
}
.media-stage {
  background: #001e37;
  min-height: 200px;
  position: relative;
  overflow: auto;
  text-align: center;
  border: 1px solid #214d70;
}
.media-stage img {
  display: block;
  max-width: 100%;
  max-height: 75vh;
  width: auto;
  height: auto;
  margin: auto;
  touch-action: pan-x pan-y pinch-zoom;
}
.media-stage.actual-size {
  max-height: 80vh;
}
.media-stage.actual-size img {
  max-width: none;
  max-height: none;
  margin: 0;
}
.media-stage video {
  display: block;
  max-width: 100%;
  max-height: 75vh;
  margin: auto;
  min-width: min(100%, 400px);
}
.media-loading {
  padding: 20px;
  color: #b6cddd;
  font-size: 13px;
}
.detail-review {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
  padding: 14px 0;
  position: sticky;
  bottom: 0;
  background: #012e56f2;
  border-top: 1px solid var(--line);
  margin-top: 18px;
}
.detail-review p {
  font-size: 12px;
  margin: 6px 0 0;
}
.provenance {
  border-top: 1px solid #315572;
  padding-top: 18px;
  margin-top: 20px;
}
.review-target {
  margin-top: 18px;
  color: var(--muted);
}
.review-target strong {
  color: #f5cf77;
  font-size: 18px;
}
.provenance p {
  overflow-wrap: anywhere;
  font-family: monospace;
  font-size: 11px;
}
footer {
  padding: 20px 28px;
  background: #002442;
  font-size: 11px;
  color: #8caac0;
  text-align: center;
}
@media (min-width: 1500px) {
  .workspace {
    grid-template-columns: 250px minmax(0, 1fr);
  }
  .main-content {
    padding: 28px 36px;
  }
}
@media (max-width: 760px) {
  .site-header {
    height: 58px;
    padding: 10px 16px;
    gap: 20px;
  }
  .wordmark {
    font-size: 29px;
  }
  .wordmark span {
    font-size: 9px;
  }
  .site-header nav span {
    display: none;
  }
  .site-header nav {
    font-size: 12px;
  }
  .site-header > button {
    font-size: 11px;
    padding: 7px;
  }
  .workspace {
    display: block;
  }
  .sidebar {
    padding: 16px;
    border-right: 0;
    border-bottom: 1px solid #315572;
  }
  .sidebar h2 {
    margin: 10px 0;
  }
  .sidebar > p {
    margin-bottom: 8px;
  }
  .count-grid {
    grid-template-columns: repeat(4, 1fr);
    margin: 12px 0;
  }
  .count-grid strong {
    font-size: 20px;
  }
  .count-grid button {
    font-size: 10px;
    padding: 8px;
  }
  .filters {
    display: block;
  }
  .filters .filter-fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px 12px;
  }
  .filters summary {
    grid-column: 1/-1;
    margin: 5px 0;
  }
  .filters label {
    margin-top: 4px;
  }
  .sidebar-tags {
    display: none;
  }
  .main-content {
    padding: 18px 12px;
  }
  .page-heading {
    align-items: start;
  }
  .page-heading h1 {
    font-size: 22px;
  }
  .page-heading h1 small {
    display: block;
    font-size: 12px;
    margin-top: 5px;
  }
  .page-heading .primary {
    font-size: 12px;
    white-space: nowrap;
  }
  .eyebrow {
    font-size: 9px;
  }
  .pill {
    display: none;
  }
  .search-bar {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
  .search-input {
    grid-column: 1/-1;
    min-width: 0;
  }
  .search-bar > label:not(.search-input) {
    max-width: none;
  }
  .search-bar .check {
    grid-column: 1/-1;
  }
  .tag-result {
    padding: 14px 7px;
  }
  .tag-result button {
    font-size: 12px;
    padding: 8px;
  }
  .tag-result h3 {
    font-size: 14px;
  }
  .gallery-scroll {
    height: 65vh;
  }
  .gallery-row {
    gap: 8px;
  }
  .card-meta {
    font-size: 9px;
  }
  .card-top {
    font-size: 9px;
  }
  .card-top .check {
    font-size: 10px;
  }
  .focused-review {
    display: block;
  }
  .focused-review > span {
    display: block;
    margin-bottom: 8px;
  }
  .review-buttons button {
    min-height: 44px;
    padding: 10px;
    font-size: 12px;
  }
  .quick-labels button {
    min-height: 44px;
  }
  .post-navigation {
    gap: 8px;
    font-size: 11px;
    margin-bottom: 18px;
  }
  .post-navigation button {
    font-size: 11px;
    padding: 9px;
  }
  .post-content .page-heading {
    flex-wrap: wrap;
  }
  .detail-review {
    padding: 12px 0;
    gap: 10px;
  }
  .detail-review .review-block {
    gap: 8px;
  }
  .detail-review .review-buttons {
    flex: 1;
  }
  .detail-review .review-buttons button {
    flex: 1;
  }
  .detail-review > div {
    width: 100%;
  }
  .detail-layout .sidebar .detail-tags {
    max-height: 240px;
    overflow: auto;
  }
  .detail-layout .sidebar > p {
    font-size: 11px;
  }
  .home-layout .sidebar {
    max-height: 230px;
    overflow: auto;
  }
  .media-stage img,
  .media-stage video {
    max-height: 65vh;
  }
  .save-errors {
    padding: 10px;
    font-size: 12px;
  }
  footer {
    padding: 16px;
    font-size: 10px;
  }
}
@media (prefers-reduced-motion: no-preference) {
  button,
  a {
    transition:
      background-color 0.12s,
      border-color 0.12s;
  }
}
.dataset-page {
  max-width: 1100px;
  margin: 0 auto;
  padding: 18px;
}
.dataset-page section {
  margin: 24px 0;
  padding: 18px;
  border: 1px solid #365779;
  border-radius: 3px;
}
.dataset-columns {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
  gap: 20px;
}
.dataset-page dl > div {
  display: flex;
  justify-content: space-between;
  gap: 16px;
}
.dataset-page form {
  display: grid;
  gap: 14px;
  max-width: 700px;
}
.dataset-page form > label:not(.check) {
  display: grid;
  gap: 6px;
}
.dataset-page .count-grid > div {
  padding: 12px;
  display: grid;
  gap: 8px;
}
.dataset-version {
  border-top: 1px solid #365779;
  padding: 12px 0;
}
.dataset-version nav {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
}
.dataset-hash {
  overflow-wrap: anywhere;
  font-size: 12px;
}
.additional-search {
  flex-wrap: wrap;
}

/* Keep optional evidence below the original, outside the review control bar. */
.embedding-assist {
  margin: 1.5rem 0;
  padding: 1rem;
  border: 1px solid var(--line);
  border-radius: 3px;
  overflow-wrap: anywhere;
}
.neighbor-list {
  max-height: 18rem;
  overflow-y: auto;
  margin: 1rem 0;
}
.neighbor-row {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 0.7rem;
  padding: 0.5rem;
}
.neighbor-row input {
  width: auto;
  flex: 0 0 auto;
}

.favorite-control {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
  font-size: 12px;
}
.favorite-button {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
}
.favorite-button.hearted {
  color: #ffa5bf;
  border-color: #ca7d97;
}
.favorite-button > span:first-child {
  font-size: 19px;
  line-height: 1;
}
.favorite-control.compact {
  display: inline-flex;
  flex: 1;
  gap: 2px;
}
.favorite-control.compact .favorite-button {
  width: 100%;
  justify-content: center;
  min-height: 32px;
  padding: 2px;
}
.favorite-error {
  color: #ffc2b3;
  overflow-wrap: anywhere;
}

/* Query controls stay above the scroll viewport so suggestions never get clipped. */
.query-input {
  position: relative;
  flex: 1;
  min-width: min(100%, 220px);
}
.query-input input {
  max-width: none;
  margin: 0;
}
.query-suggestions {
  position: absolute;
  z-index: 10;
  top: 100%;
  left: 0;
  right: 0;
  margin: 4px 0 0;
  padding: 4px;
  list-style: none;
  background: #062440;
  border: 1px solid var(--line);
  box-shadow: 0 8px 20px #00152580;
  max-height: 280px;
  overflow: auto;
}
.query-suggestions li {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 10px;
  cursor: pointer;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.query-suggestions li[aria-selected="true"],
.query-suggestions li:hover {
  background: #174b77;
}
.query-suggestions small {
  color: var(--muted);
}
.additional-search {
  align-items: flex-start;
}
.search-options {
  position: relative;
  padding: 9px 0;
  font-size: 13px;
}
.search-options summary,
.shortcut-help summary {
  cursor: pointer;
  color: var(--accent);
}
.search-options select {
  width: 230px;
}
.shortcut-help {
  position: relative;
  font-size: 12px;
}
.shortcut-help p {
  position: absolute;
  right: 0;
  width: 230px;
  padding: 12px;
  border: 1px solid var(--line);
  background: #062440;
  z-index: 11;
  line-height: 1.7;
}
.feed-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: 12px;
  min-height: 24px;
}
.feed-status button {
  padding: 4px 10px;
}
.feed-status .error {
  margin: 0;
}
.focused-review {
  padding-top: 8px;
  gap: 12px;
}
.provenance {
  padding-top: 10px;
  margin: 10px 0;
  font-size: 12px;
}
.dataset-page h1 {
  font-size: 23px;
}
.dataset-page section {
  background: #062f51;
  margin: 16px 0;
  padding: 16px;
}
.dataset-page h2 {
  font-size: 18px;
}
@media (max-width: 760px) {
  .query-input {
    flex-basis: calc(100% - 90px);
  }
  .query-suggestions li {
    min-height: 44px;
    align-items: center;
  }
  .search-options {
    width: 100%;
    padding: 0;
  }
  .search-options select {
    width: 100%;
  }
  .site-header .wordmark span {
    display: none;
  }
  .gallery-content {
    gap: 8px;
  }
  .feed-status button {
    min-height: 44px;
  }
  .favorite-control.compact .favorite-button {
    min-height: 44px;
  }
}
```
