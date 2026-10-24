# Shared components

Framework: React 19 + TypeScript, custom components, vanilla CSS; no component library. Full source follows.

## ReviewProvider and ReviewButtons

- Source: `frontend/src/components/Reviews.tsx`
- Description: Review state, optimistic saves, retries, undo, and shared label controls.
- Key props: ReviewButtons: project, post, onSaved; ReviewProvider: children.

### `frontend/src/components/Reviews.tsx`

```tsx
import {
  createContext,
  useContext,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { recordMetric } from "../api/telemetry";
import {
  api,
  errorText,
  mutationId,
  type Candidate,
  type CandidatePage,
  type LabelResult,
  type LabelState,
} from "../api/client";

type Operation = {
  project: number;
  posts: number[];
  run: () => Promise<LabelResult[]>;
  optimistic: Record<number, LabelState | null>;
  undoOf?: number;
};
type UndoTarget = Pick<Candidate, "post_id" | "state" | "version" | "event_id">;
type HistoryEntry = { project: number; result: LabelResult };
type Pending = {
  state: LabelState | null;
  saving: boolean;
  error?: string;
  retry?: () => void;
};
type ReviewContext = {
  pending: Record<string, Pending>;
  edit: (
    project: number,
    posts: Candidate[],
    state: LabelState,
  ) => Promise<boolean>;
  undo: (project: number, post: UndoTarget) => Promise<boolean>;
  undoLast: (project: number, fallback: UndoTarget) => Promise<number | null>;
};
const Context = createContext<ReviewContext | null>(null);
export const useReviews = () => useContext(Context)!;
const keyFor = (project: number, post: number) => `${project}:${post}`;

export function ReviewProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const active = useRef(new Set<string>());
  const history = useRef<HistoryEntry[]>(readHistory());
  const [pending, setPending] = useState<Record<string, Pending>>({});

  async function execute(operation: Operation): Promise<boolean> {
    const keys = operation.posts.map((id) => keyFor(operation.project, id));
    if (keys.some((key) => active.current.has(key))) return false;
    keys.forEach((key) => active.current.add(key));
    setPending((old) => ({
      ...old,
      ...Object.fromEntries(
        operation.posts.map((id) => [
          keyFor(operation.project, id),
          { state: operation.optimistic[id], saving: true },
        ]),
      ),
    }));
    try {
      const started = performance.now();
      const results = await operation.run();
      recordMetric("label_ack_ms", performance.now() - started);
      history.current = operation.undoOf
        ? history.current.filter(
            (entry) => entry.result.event_id !== operation.undoOf,
          )
        : [
            ...history.current,
            ...results.map((result) => ({
              project: operation.project,
              result,
            })),
          ].slice(-50);
      // A disabled/full browser storage area must not turn an acknowledged save into a failure.
      try {
        sessionStorage.setItem(
          "studio-undo-history",
          JSON.stringify(history.current),
        );
      } catch {
        /* In-memory undo remains available. */
      }
      results.forEach((result) => {
        client.setQueryData<Candidate>(
          ["post", operation.project, result.post_id],
          (old) => (old ? { ...old, ...result } : undefined),
        );
        client.setQueriesData<InfiniteData<CandidatePage>>(
          { queryKey: ["candidates", operation.project] },
          (old) =>
            old
              ? {
                  ...old,
                  pages: old.pages.map((page) => ({
                    ...page,
                    items: page.items.map((item) =>
                      item.post_id === result.post_id
                        ? { ...item, ...result }
                        : item,
                    ),
                  })),
                }
              : undefined,
        );
      });
      await Promise.all([
        client.invalidateQueries({ queryKey: ["project", operation.project] }),
        client.invalidateQueries({
          queryKey: ["candidates", operation.project],
        }),
      ]);
      setPending((old) =>
        Object.fromEntries(
          Object.entries(old).filter(([key]) => !keys.includes(key)),
        ),
      );
      return true;
    } catch (error) {
      setPending((old) => ({
        ...old,
        ...Object.fromEntries(
          operation.posts.map((id) => [
            keyFor(operation.project, id),
            {
              state: operation.optimistic[id],
              saving: false,
              error: errorText(error),
              retry: () => {
                void execute(operation);
              },
            },
          ]),
        ),
      }));
      void client.invalidateQueries({ queryKey: ["post", operation.project] });
      void client.invalidateQueries({
        queryKey: ["candidates", operation.project],
      });
      return false;
    } finally {
      keys.forEach((key) => active.current.delete(key));
    }
  }

  async function edit(project: number, posts: Candidate[], state: LabelState) {
    const request_id = mutationId();
    const items = posts.map((post) => ({
      post_id: post.post_id,
      expected_version: post.version,
      state,
    }));
    return execute({
      project,
      posts: posts.map((p) => p.post_id),
      optimistic: Object.fromEntries(posts.map((p) => [p.post_id, state])),
      run: () =>
        posts.length === 1
          ? api<LabelResult>(
              `/projects/${project}/labels/${posts[0].post_id}`,
              {
                method: "PATCH",
                body: JSON.stringify({ request_id, ...items[0] }),
              },
            ).then((value) => [value])
          : api<LabelResult[]>(`/projects/${project}/labels/batch`, {
              method: "POST",
              body: JSON.stringify({ request_id, items }),
            }),
    });
  }

  async function undo(project: number, post: UndoTarget) {
    if (!post.event_id) return false;
    const request_id = mutationId();
    return execute({
      project,
      posts: [post.post_id],
      optimistic: { [post.post_id]: post.state },
      undoOf: post.event_id,
      run: () =>
        api<LabelResult>(`/projects/${project}/labels/${post.post_id}/undo`, {
          method: "POST",
          body: JSON.stringify({
            request_id,
            event_id: post.event_id,
            expected_version: post.version,
          }),
        }).then((value) => [value]),
    });
  }

  async function undoLast(project: number, fallback: UndoTarget) {
    const latest =
      [...history.current].reverse().find((entry) => entry.project === project)
        ?.result ?? fallback;
    return (await undo(project, latest)) ? latest.post_id : null;
  }

  const failures = Object.entries(pending).filter(([, value]) => value.error);
  return (
    <Context.Provider value={{ pending, edit, undo, undoLast }}>
      {children}
      {failures.length > 0 && (
        <aside className="save-errors" role="alert">
          <strong>{failures.length} save(s) need attention</strong>
          {failures.map(([key, value]) => (
            <div key={key}>
              Post #{key.split(":")[1]}: {value.error}{" "}
              <button onClick={value.retry}>Retry same save</button>
              <button
                onClick={() =>
                  setPending((old) =>
                    Object.fromEntries(
                      Object.entries(old).filter(([k]) => k !== key),
                    ),
                  )
                }
              >
                Dismiss
              </button>
            </div>
          ))}
        </aside>
      )}
    </Context.Provider>
  );
}

export function ReviewButtons({
  project,
  post,
  onSaved,
}: {
  project: number;
  post: Candidate;
  onSaved?: () => void;
}) {
  const reviews = useReviews();
  const pending = reviews.pending[keyFor(project, post.post_id)];
  const state = pending?.saving ? pending.state : post.state;
  return (
    <div className="review-block">
      <div className="review-buttons">
        {(["positive", "negative", "unsure"] as const).map((value, index) => (
          <button
            key={value}
            className={`${value} ${state === value ? "chosen" : ""}`}
            aria-pressed={state === value}
            disabled={pending?.saving || !!pending?.error}
            onClick={() => {
              void reviews.edit(project, [post], value).then((ok) => {
                if (ok) onSaved?.();
              });
            }}
          >
            <kbd>{index + 1}</kbd>{" "}
            {value === "positive"
              ? "Positive"
              : value === "negative"
                ? "Negative"
                : "Unsure"}
          </button>
        ))}
      </div>
      <span className="save-state" aria-live="polite">
        {pending?.saving
          ? "Saving…"
          : pending?.error
            ? "Save failed — retry below"
            : post.state
              ? "Saved"
              : "Unreviewed"}
      </span>
      <button
        className="quiet"
        disabled={!post.event_id || pending?.saving || !!pending?.error}
        onClick={() => {
          void reviews.undo(project, post);
        }}
      >
        Undo this post
      </button>
    </div>
  );
}

function readHistory(): HistoryEntry[] {
  try {
    const value: unknown = JSON.parse(
      sessionStorage.getItem("studio-undo-history") ?? "[]",
    );
    if (!Array.isArray(value)) return [];
    return value
      .filter(
        (entry) =>
          typeof entry?.project === "number" &&
          typeof entry?.result?.event_id === "number" &&
          typeof entry.result.version === "number" &&
          typeof entry.result.post_id === "number",
      )
      .slice(-50);
  } catch {
    return [];
  }
}
```

## FavoriteButton

- Source: `frontend/src/components/FavoriteButton.tsx`
- Description: Collection-save toggle with compact gallery mode.
- Key props: project, post, compact.

### `frontend/src/components/FavoriteButton.tsx`

```tsx
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  errorText,
  mutationId,
  type Candidate,
} from "../api/client";
import type { components } from "../api/schema";

type Saved = components["schemas"]["SaveState"];
type Capability = components["schemas"]["LibraryCapability"];
const active = new Set([
  "queued",
  "downloading",
  "publishing",
  "removing",
  "retrying",
]);

export function FavoriteButton({
  project,
  post,
  compact = false,
}: {
  project: number;
  post: Candidate;
  compact?: boolean;
}) {
  // Older Studio servers do not advertise collection saving in candidate data.
  // Keep the tagging UI usable without requiring an unrelated schema upgrade.
  if (!post.favorite) return null;
  return (
    <AvailableFavoriteButton project={project} post={post} compact={compact} />
  );
}

function AvailableFavoriteButton({
  project,
  post,
  compact = false,
}: {
  project: number;
  post: Candidate;
  compact?: boolean;
}) {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState<(() => Promise<void>) | null>(null);
  const key = ["favorite", post.post_id];
  const capability = useQuery({
    queryKey: ["library-capability"],
    queryFn: () => api<Capability>("/library/status"),
    staleTime: 30_000,
  });
  const query = useQuery({
    queryKey: key,
    queryFn: () => api<Saved>(`/library/posts/${post.post_id}`),
    initialData: post.favorite,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchInterval: (q) =>
      active.has(q.state.data?.status ?? "") ? 1000 : false,
  });
  useEffect(() => {
    client.setQueryData<Saved>(key, (old) =>
      !old || post.favorite.updated_at > old.updated_at ? post.favorite : old,
    );
  }, [client, post.favorite, post.post_id]);
  const saved = query.data;
  const waiting = active.has(saved.status);
  async function change(wanted: boolean) {
    const body = {
      request_id: mutationId(),
      expected_version: saved.version,
      wanted,
    };
    async function perform() {
      setBusy(true);
      setError("");
      setRetry(null);
      try {
        const value = await api<Saved>(
          `/projects/${project}/library/${post.post_id}`,
          { method: "PUT", body: JSON.stringify(body) },
        );
        client.setQueryData(key, value);
        await query.refetch();
      } catch (e) {
        setError(errorText(e));
        if (e instanceof ApiError && e.status === 409) await query.refetch();
        else setRetry(() => perform);
      } finally {
        setBusy(false);
      }
    }
    await perform();
  }
  const label = saved.wanted
    ? `Remove saved post ${post.post_id} from Smut folder`
    : `Save post ${post.post_id} to Smut folder`;
  const message =
    error ||
    saved.error ||
    (capability.data?.available ? "" : capability.data?.message);
  return (
    <div className={`favorite-control ${compact ? "compact" : ""}`}>
      <button
        className={`favorite-button ${saved.wanted ? "hearted" : ""}`}
        aria-pressed={saved.wanted}
        aria-label={label}
        title={message || label}
        disabled={
          busy ||
          !capability.data?.configured ||
          (!saved.wanted &&
            !saved.present &&
            (!post.has_original || post.unavailable))
        }
        onClick={(e) => {
          e.stopPropagation();
          void change(!saved.wanted);
        }}
      >
        <span aria-hidden="true">{saved.wanted ? "♥" : "♡"}</span>
        {!compact && (
          <span>
            {waiting
              ? saved.wanted
                ? "Saving…"
                : "Removing…"
              : saved.status === "saved"
                ? "Saved to Smut"
                : saved.wanted
                  ? "Save requested"
                  : "Save to Smut"}
          </span>
        )}
        {compact && waiting && (
          <span aria-label="Save operation pending">…</span>
        )}
      </button>
      {!compact && saved.status === "saved" && (
        <span role="status">Original and e621 tags saved.</span>
      )}
      {message && (
        <span className="favorite-error" role="alert" title={message}>
          {compact ? "!" : message}
        </span>
      )}
      {retry && (
        <button disabled={busy} onClick={() => void retry()}>
          Retry save request
        </button>
      )}
      {saved.status === "failed" && !error && (
        <button
          disabled={busy}
          aria-label={`Retry collection operation for post ${post.post_id}`}
          onClick={() => void change(saved.wanted)}
        >
          Retry
        </button>
      )}
    </div>
  );
}
```

## QueryInput

- Source: `frontend/src/components/QueryInput.tsx`
- Description: e621 query text field with tag suggestions and keyboard selection.
- Key props: value, onChange, label, placeholder.

### `frontend/src/components/QueryInput.tsx`

```tsx
import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Tag } from "../api/client";

const categories = [
  "general",
  "artist",
  "contributor",
  "copyright",
  "character",
  "species",
  "invalid",
  "meta",
  "lore",
];
const metatags: Record<string, string[]> = {
  rating: ["s", "q", "e"],
  type: ["jpg", "png", "gif", "webm", "mp4", "swf"],
};

export function QueryInput({
  value,
  onChange,
  label,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const [caret, setCaret] = useState(value.length);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const start = value.slice(0, caret).search(/\S+$/);
  const endMatch = value.slice(caret).search(/\s/);
  const end = endMatch < 0 ? value.length : caret + endMatch;
  const token = start < 0 ? "" : value.slice(start, caret);
  const sign = /^[-~]/.test(token) ? token[0] : "";
  const plain = token.slice(sign.length);
  const colon = plain.indexOf(":");
  const qualifier = colon >= 0 ? plain.slice(0, colon).toLowerCase() : "";
  const term = colon >= 0 ? plain.slice(colon + 1) : plain;
  const tagQuery = colon < 0 || categories.includes(qualifier);
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    setActive(-1);
    const timer = setTimeout(() => setDebounced(term.toLowerCase()), 250);
    return () => clearTimeout(timer);
  }, [term, qualifier]);
  const suggestions = useQuery({
    queryKey: ["autocomplete", debounced],
    enabled:
      open &&
      tagQuery &&
      !!debounced &&
      debounced === term.toLowerCase() &&
      !/[\s*]/.test(debounced) &&
      debounced.length <= 100,
    queryFn: ({ signal }) =>
      api<Tag[]>(`/tags/autocomplete?q=${encodeURIComponent(debounced)}`, {
        signal,
      }),
    staleTime: 60 * 60 * 1000,
    retry: false,
  });
  const choices = tagQuery
    ? debounced === term.toLowerCase()
      ? (suggestions.data ?? [])
      : []
    : (metatags[qualifier] ?? [])
        .filter((item) => item.startsWith(term))
        .map((name) => ({
          canonical_name: name,
          matched_alias: null,
          category: 7,
          post_count: 0,
          tag_id: 0,
        }));
  const shown = open && !!token && choices.length > 0;
  function choose(name: string, retainFocus = true) {
    const replacement =
      sign + (colon >= 0 && !tagQuery ? qualifier + ":" : "") + name;
    const suffix = value.slice(end);
    const next = value.slice(0, start) + replacement + (suffix || " ");
    const position = start + replacement.length + (suffix ? 0 : 1);
    onChange(next);
    setCaret(position);
    setOpen(false);
    requestAnimationFrame(() => {
      if (retainFocus) input.current?.focus();
      input.current?.setSelectionRange(position, position);
    });
  }
  return (
    <div className="query-input">
      <input
        ref={input}
        value={value}
        maxLength={500}
        placeholder={placeholder}
        aria-label={label}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={shown}
        aria-controls={shown ? id : undefined}
        aria-activedescendant={
          shown && active >= 0 ? `${id}-${active}` : undefined
        }
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => {
          onChange(event.target.value);
          setCaret(event.target.selectionStart ?? 0);
          setOpen(true);
        }}
        onSelect={(event) => setCaret(event.currentTarget.selectionStart ?? 0)}
        onKeyUp={(event) => setCaret(event.currentTarget.selectionStart ?? 0)}
        onClick={(event) => setCaret(event.currentTarget.selectionStart ?? 0)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === "Escape") {
            setOpen(false);
            return;
          }
          if (!shown) return;
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setActive(
              (old) =>
                (old +
                  (event.key === "ArrowDown" ? 1 : choices.length - 1) +
                  choices.length) %
                choices.length,
            );
          } else if (
            (event.key === "Enter" || event.key === "Tab") &&
            active >= 0 &&
            choices[active]
          ) {
            if (event.key === "Enter") event.preventDefault();
            choose(choices[active].canonical_name, event.key !== "Tab");
          }
        }}
      />
      {shown && (
        <ul
          id={id}
          role="listbox"
          aria-label="Tag suggestions"
          className="query-suggestions"
        >
          {choices.map((tag, index) => (
            <li
              key={`${tag.canonical_name}:${tag.matched_alias}`}
              id={`${id}-${index}`}
              role="option"
              aria-selected={active === index}
              className={`tag-${categories[tag.category] ?? "general"}`}
              onPointerDown={(event) => event.preventDefault()}
              onClick={() => choose(tag.canonical_name)}
            >
              <span>
                {tag.matched_alias && <small>{tag.matched_alias} → </small>}
                {tag.canonical_name}
              </span>
              {!!tag.post_count && (
                <small>{tag.post_count.toLocaleString()}</small>
              )}
            </li>
          ))}
        </ul>
      )}
      {open && suggestions.isError && tagQuery && (
        <small role="status">
          Suggestions unavailable. You can still search.
        </small>
      )}
    </div>
  );
}
```

## TagSidebar

- Source: `frontend/src/components/TagSidebar.tsx`
- Description: Category-colored e621 tags with optional hiding.
- Key props: tags, hidden.

### `frontend/src/components/TagSidebar.tsx`

```tsx
import type { Candidate } from "../api/client";

const order = [
  "artist",
  "copyright",
  "character",
  "species",
  "general",
  "meta",
  "lore",
  "contributor",
  "invalid",
];
export function TagSidebar({
  tags,
  hidden,
}: {
  tags: Candidate["tags"];
  hidden: boolean;
}) {
  if (hidden) return <p className="muted">Tags hidden while reviewing.</p>;
  return (
    <div className="tag-list">
      {order
        .filter((category) => tags[category]?.length)
        .map((category) => (
          <section key={category}>
            <h3>{category}</h3>
            {tags[category].map((tag) => (
              <a
                className={`tag-${category}`}
                key={tag}
                href={`https://e621.net/posts?tags=${encodeURIComponent(tag)}`}
                target="_blank"
                rel="noreferrer"
              >
                {tag.replaceAll("_", " ")}
              </a>
            ))}
          </section>
        ))}
    </div>
  );
}
```

## Thumbnail

- Source: `frontend/src/components/Thumbnail.tsx`
- Description: Candidate preview and loading state.
- Key props: id, available, onLoaded.

### `frontend/src/components/Thumbnail.tsx`

```tsx
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { mediaUrl } from "../api/client";

export function Thumbnail({
  id,
  available = true,
  onLoaded,
}: {
  id: number;
  available?: boolean;
  onLoaded?: () => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["thumbnail", id],
    enabled: available,
    staleTime: Infinity,
    gcTime: 30_000,
    retry: false,
    queryFn: async ({ signal }) => {
      const response = await fetch(mediaUrl(id), { signal });
      if (response.status === 202) return null;
      if (!response.ok) throw new Error("Thumbnail unavailable");
      return response.blob();
    },
    refetchInterval: (query) => (query.state.data === null ? 1000 : false),
  });
  useEffect(() => {
    const value = query.data ? URL.createObjectURL(query.data) : null;
    setUrl(value);
    return () => {
      if (value) URL.revokeObjectURL(value);
    };
  }, [query.data]);
  if (!available || query.error)
    return <span className="thumbnail-placeholder">Preview unavailable</span>;
  return url ? (
    <img src={url} alt={`Post ${id}`} draggable={false} onLoad={onLoaded} />
  ) : (
    <span className="thumbnail-placeholder">Loading preview…</span>
  );
}
```

## EmbeddingAssist

- Source: `frontend/src/components/EmbeddingAssist.tsx`
- Description: Similar-post evidence, neighbor import, and source tag suggestions.
- Key props: project, post, hideTags.

### `frontend/src/components/EmbeddingAssist.tsx`

```tsx
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, errorText, mutationId } from "../api/client";
import type { components } from "../api/schema";

type Evidence = components["schemas"]["NeighborEvidence"];
type Status = components["schemas"]["EmbeddingStatus"];
type ImportResult = components["schemas"]["NeighborImportResult"];

export function EmbeddingAssist({
  project,
  post,
  hideTags,
}: {
  project: number;
  post: number;
  hideTags: boolean;
}) {
  const path = `/projects/${project}/neighbors/${post}`;
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [imported, setImported] = useState<ImportResult | null>(null);
  const [retry, setRetry] = useState<(() => Promise<void>) | null>(null);
  const status = useQuery({
    queryKey: ["embedding-status"],
    queryFn: () => api<Status>("/embeddings/status"),
  });
  const evidence = useQuery({
    queryKey: ["neighbors", project, post],
    queryFn: () => api<Evidence>(path),
    refetchInterval: (query) =>
      ["pending", "running"].includes(query.state.data?.status ?? "")
        ? 1500
        : false,
  });
  const data = evidence.data;
  const running = ["pending", "running"].includes(data?.status ?? "");
  const result = data?.result;
  async function perform(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    setRetry(null);
    try {
      await operation();
    } catch (e) {
      setError(errorText(e));
      setRetry(() => () => perform(operation));
    } finally {
      setBusy(false);
    }
  }
  function fetchNeighbors() {
    const body = { request_id: mutationId() };
    void perform(async () => {
      await api(path, { method: "POST", body: JSON.stringify(body) });
      await evidence.refetch();
    });
  }
  function importSelected() {
    const body = {
      request_id: mutationId(),
      job_id: data?.job_id,
      post_ids: [...selected],
    };
    void perform(async () => {
      const outcome = await api<ImportResult>(path + "/import", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setImported(outcome);
      setSelected(new Set());
    });
  }
  return (
    <section className="embedding-assist" aria-label="Embedding assistance">
      <h2>Similar posts & tag suggestions</h2>
      <p>
        Explore Hydra’s published neighbors for this post. Your positive,
        negative and unsure reviews remain the ground truth.
      </p>
      {status.data && <p className="muted">{status.data.message}</p>}
      {(status.error || evidence.error) && (
        <p role="alert" className="error">
          {errorText(status.error || evidence.error)}
        </p>
      )}
      <button
        disabled={busy || running || !status.data?.enabled}
        onClick={fetchNeighbors}
      >
        {running
          ? "Finding published neighbors…"
          : data?.status === "done"
            ? "Refresh neighbor evidence"
            : "Find similar posts"}
      </button>
      {running && (
        <p role="status">
          Lookup runs separately; you can keep reviewing. Results survive
          reloads.
        </p>
      )}
      {data?.error && (
        <p className="error" role="alert">
          {data.error}
        </p>
      )}
      {result?.coverage === "not_found" && (
        <p role="status">{String(result.message)}</p>
      )}
      {data?.status === "done" && data.neighbors.length > 0 && (
        <>
          <p className="muted">
            Distance is publisher L2 (lower is nearer), not a probability or a
            label. The index is a July 2026 snapshot.
          </p>
          <div className="neighbor-list">
            {data.neighbors.map((item) => (
              <label className="neighbor-row" key={item.post_id}>
                <input
                  type="checkbox"
                  aria-label={`Select neighbor ${item.post_id}`}
                  checked={selected.has(item.post_id)}
                  disabled={
                    busy || (!selected.has(item.post_id) && selected.size >= 50)
                  }
                  onChange={(e) =>
                    setSelected((previous) => {
                      const next = new Set(previous);
                      if (e.target.checked) next.add(item.post_id);
                      else next.delete(item.post_id);
                      return next;
                    })
                  }
                />
                <span>
                  #{item.post_id} · {item.distance.toFixed(3)} ·{" "}
                  {item.in_project
                    ? (item.state ?? "unreviewed")
                    : "not imported"}
                  {item.unavailable && " · unavailable"}
                  {item.in_project && (
                    <>
                      {" "}
                      ·{" "}
                      <Link to={`/projects/${project}/posts/${item.post_id}`}>
                        Review post
                      </Link>
                    </>
                  )}
                </span>
              </label>
            ))}
          </div>
          <button
            disabled={busy || selected.size === 0}
            onClick={importSelected}
          >
            Import selected neighbors ({selected.size}/50)
          </button>
          <p>
            Imports append discovery evidence and preserve existing labels.
            Inspect originals before reviewing.
          </p>
          {hideTags ? (
            <p>Tag suggestions are hidden for review.</p>
          ) : (
            <>
              <h3>Suggested tags from neighbor metadata</h3>
              <p>
                {data.known_metadata}/{data.neighbors.length} neighbors have
                available local metadata. Counts describe source tags, not Hydra
                predictions or confidence.
              </p>
              {data.suggested_tags.length ? (
                <ul>
                  {data.suggested_tags.map((tag) => (
                    <li key={tag.name}>
                      {tag.name} · {tag.count}/{data.known_metadata}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>
                  Import neighbors to inspect recurring tags. No tag is assigned
                  automatically.
                </p>
              )}
              <button onClick={() => void evidence.refetch()}>
                Refresh tag suggestions
              </button>
            </>
          )}
          <details>
            <summary>Embedding provenance</summary>
            <p>Published revision: {String(result?.revision)}</p>
            <p>Index row: {String(result?.row)}</p>
            <p className="dataset-hash">
              Row artifact SHA-256: {String(result?.artifact_sha256)}
            </p>
            <p>{String(result?.verification)}</p>
          </details>
        </>
      )}
      {imported && (
        <p role="status">
          Neighbor import queued.{" "}
          <Link to={`/projects/${project}?run_id=${imported.run_id}`}>
            Open these search results
          </Link>
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}{" "}
          {retry && (
            <button disabled={busy} onClick={() => void retry()}>
              Retry same request
            </button>
          )}
        </p>
      )}
    </section>
  );
}
```
