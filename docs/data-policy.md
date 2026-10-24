# Data policy

Studio stores reviewed-post identity/metadata and provenance, project-specific human labels, audit events, retry receipts, sessions and explicit download journals under its configured data root. Fresh unreviewed query results stay in bounded expiring memory. Browsing media is never retained on the server. Plaintext administrator passwords and API keys are not stored in SQLite or returned to the browser.

Raw e621 tags are discovery evidence. Every candidate starts unreviewed. The reviewer confirms positive, negative or unsure; tag absence never implies a verified negative. Skip leaves the label unchanged. Undo appends history rather than deleting it.

Images and videos stream privately with no-store responses when browsed or opened. Reviewing never saves media. Double-tap/heart explicitly saves an original plus e621 JSON/TXT/XMP sidecars to the configured collection; it is idempotent and never changes labels. Removing collection files is a separately named action that verifies exact ownership; clearing a review does not remove them. Explicit dataset-media export uses the same queue but a separate owned export directory, with checksummed bytes and a reproducible manifest/bundle. Metadata export downloads no originals. Numeric preview fingerprints may be retained for reviewed duplicate analysis, without preview files. Legacy caches and imported-only rows are preserved pending a separate approved cleanup plan. Frozen versions are immutable; unsure/conflicting reviews are excluded from supervision, and never-reviewed query results are absent. No multi-tag training interface is claimed. See [dataset policy](datasets.md).

Use private HTTPS. Authentication protects metadata, media, jobs and metrics. Session cookies are HttpOnly/SameSite Strict, with Secure enabled by default. Mutations require the configured Origin and session CSRF token. Secrets belong in the ignored environment file.

Private passwordless access is optional and disabled by default. With `STUDIO_PASSWORDLESS=true`, anyone allowed through the LAN/VPN proxy can obtain a session automatically. The proxy's network restrictions become the access boundary; session, Origin and CSRF protections still apply.

The owner may explicitly choose private HTTP with `STUDIO_SECURE_COOKIE=false`; transport encryption and certificate verification are then deferred. HttpOnly/SameSite cookies, sessions and Origin/CSRF checks remain enabled.

Media URLs must originate in e621 API responses; clients cannot submit arbitrary fetch URLs. Redirects undergo the same allowlist validation. Studio never modifies e621. Respect the [official API conventions](https://e621.net/help/api) and the rights attached to individual media. Do not redistribute cached media by default.
