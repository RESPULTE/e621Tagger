# Data policy

Studio stores source metadata, discovery provenance, project-specific human labels, audit events, retry receipts, sessions and thumbnail derivatives under its configured data root. Paths in SQLite are relative. Plaintext administrator passwords and API keys are not stored in SQLite or returned to the browser.

Raw e621 tags are discovery evidence. Every candidate starts unreviewed. The reviewer confirms positive, negative or unsure; tag absence never implies a verified negative. Skip leaves the label unchanged. Undo appends history rather than deleting it.

Original images and videos stream privately when opened and are not persistently cached. Thumbnails have a disk quota and are disposable. Reviewed thumbnails have eviction preference but are not archival dataset assets. Snapshots, exports, splits and asset pinning are deferred to P3.

Use private HTTPS. Authentication protects metadata, media, jobs and metrics. Session cookies are HttpOnly/SameSite Strict, with Secure enabled by default. Mutations require the configured Origin and session CSRF token. Secrets belong in the ignored environment file.

Media URLs must originate in e621 API responses; clients cannot submit arbitrary fetch URLs. Redirects undergo the same allowlist validation. Studio never modifies e621. Respect the [official API conventions](https://e621.net/help/api) and the rights attached to individual media. Do not redistribute cached media by default.
