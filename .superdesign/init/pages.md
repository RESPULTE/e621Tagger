# Page dependency trees

Trees include all relative local imports recursively; external React, React Router, TanStack Query, and virtualizer packages are omitted. `frontend/src/api/schema.d.ts` supplies generated OpenAPI types used by the API client. Authenticated routes share `App` and `ReviewProvider` (see `layouts.md` and `routes.md`).

## `/` — Home / project discovery
Entry: `frontend/src/pages/Home.tsx`
Dependencies:
- `frontend/src/components/QueryInput.tsx`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`
- `frontend/src/api/client.ts`
  - `frontend/src/api/schema.d.ts`

## `/projects/:id` — Candidate gallery and review
Entry: `frontend/src/pages/Project.tsx`
Dependencies:
- `frontend/src/components/Reviews.tsx`
  - `frontend/src/api/telemetry.ts`
    - `frontend/src/api/client.ts`
      - `frontend/src/api/schema.d.ts`
- `frontend/src/components/Thumbnail.tsx`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`
- `frontend/src/components/FavoriteButton.tsx`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`
- `frontend/src/components/QueryInput.tsx`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`
- `frontend/src/hooks/useAutoDiscovery.ts`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`
- `frontend/src/components/TagSidebar.tsx`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`
- `frontend/src/api/telemetry.ts`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`

## `/projects/:id/posts/:postId` — Original post review
Entry: `frontend/src/pages/Post.tsx`
Dependencies:
- `frontend/src/api/client.ts`
  - `frontend/src/api/schema.d.ts`
- `frontend/src/components/Reviews.tsx`
  - `frontend/src/api/telemetry.ts`
    - `frontend/src/api/client.ts`
      - `frontend/src/api/schema.d.ts`
- `frontend/src/components/TagSidebar.tsx`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`
- `frontend/src/components/EmbeddingAssist.tsx`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`
- `frontend/src/components/FavoriteButton.tsx`
  - `frontend/src/api/client.ts`
    - `frontend/src/api/schema.d.ts`

## `/projects/:id/datasets` — Dataset quality and export
Entry: `frontend/src/pages/Datasets.tsx`
Dependencies:
- `frontend/src/api/client.ts`
  - `frontend/src/api/schema.d.ts`

## Shared bootstrap and logo
- `frontend/src/main.tsx` mounts `frontend/src/App.tsx` and imports `frontend/src/style.css`.
- `frontend/src/App.tsx` imports all four page entries, `frontend/src/api/client.ts`, and `frontend/src/components/Reviews.tsx`.
- No separate image/SVG logo asset exists under `frontend/`; wordmark is text in App and styled in the stylesheet.
