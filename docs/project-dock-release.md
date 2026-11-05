# Project dock production release — 5 November 2026

Deployed `project-dock-5b3c9bc0a451` to the private Studio at http://192.168.0.15:8447 using the existing checksummed source-overlay workflow. No GitHub push was used; unrelated working-tree changes were not committed.

The new release explicitly removes `frontend/src/pages/Home.tsx`. Production serves the fox project dock build (`index-DskKi3xx.js`, `index-C6JOafi_.css`), byte-identical to the tested local build. The previous JS/CSS URLs return 404. HTML uses `Cache-Control: no-cache`. The prior application container was replaced; only the new Studio container is running.

Validation: 21 isolated Linux project/workflow/dataset checks passed before activation. The implementation's final local browser run passed 10 cases with two mobile keyboard-only skips. Production HTTP health is healthy. Mutation-heavy browser tests were not run against live data.

Schema v6 was upgraded to v7 on a new cloned volume after stopping the old writer. Before/after hashes matched for projects (6 rows), canonical mappings (3), memberships (820), labels (80), review events (93), frozen datasets (0), and saved-media records (40). The existing library mount was preserved. The first activation attempt was safely rolled back before migration because a read-only Docker mount prevented SQLite WAL verification; the retry used a read-only SQLite connection on a writable mount so SQLite could manage its shared-memory files.

Active source: `/opt/hydra-studio/releases/project-dock-5b3c9bc0a451`.
Active data volume: `hydra-studio_project-dock-5b3c9bc0a451`.
Verified backup: `/opt/hydra-studio/validation/project-dock-5b3c9bc0a451-1793875604020268807/before.sqlite3`.

Previous release and schema-v6 volume remain offline as rollback material. Do not run old code against the v7 volume or discard post-release reviews when rolling back. Live shared saved media requires reconciliation if changed after cutover.

Checksummed source plan, activation state, logs, and HTTP verification are retained locally under `artifacts/project-dock-deploy/`; the server release state is `/opt/hydra-studio/project-dock-5b3c9bc0a451.json`.

A fresh isolated live-browser session confirmed the root has no setup form, the fox dock lists project counts, and the new-project composer opens and cancels. No project/review/media mutation was performed. The initial unauthenticated session request returned the expected 401 before automatic passwordless connection. Screenshot: output/playwright/project-dock-composer.png.
