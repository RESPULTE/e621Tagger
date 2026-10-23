import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  api,
  errorText,
  mutationId,
  type Candidate,
  type CandidatePage,
  type LabelState,
  type Project as ProjectInfo,
} from "../api/client";
import { ReviewButtons, useReviews } from "../components/Reviews";
import { Thumbnail } from "../components/Thumbnail";
import { TagSidebar } from "../components/TagSidebar";
import { recordMetric, sampleFrames } from "../api/telemetry";

export function Project() {
  const projectId = Number(useParams().id);
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const reviews = useReviews();
  const scroll = useRef<HTMLDivElement>(null);
  const paint = useRef({
    started: performance.now(),
    reported: false,
    renders: 0,
  });
  paint.current.renders++;
  function reportPaint() {
    if (paint.current.reported) return;
    paint.current.reported = true;
    requestAnimationFrame(() => {
      recordMetric(
        "gallery_first_paint_ms",
        performance.now() - paint.current.started,
      );
      recordMetric("gallery_renders", paint.current.renders);
      sampleFrames();
    });
  }
  const [columns, setColumns] = useState(4);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [focused, setFocused] = useState<number | null>(null);
  const [hideTags, setHideTags] = useState(
    localStorage.getItem("studio-hide-tags") === "true",
  );
  const [discoveryOverride, setDiscoveryOverride] = useState<{
    project: number;
    query: string;
  } | null>(null);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState("");
  const [importNotice, setImportNotice] = useState("");
  const [retryImport, setRetryImport] = useState<(() => void) | null>(null);
  const requestParams = params.toString();
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api<ProjectInfo>(`/projects/${projectId}`),
    refetchInterval: 2000,
  });
  const discoveryQuery =
    discoveryOverride?.project === projectId
      ? discoveryOverride.query
      : (project.data?.query ?? "");
  const activeImport =
    project.data?.jobs.some((job) =>
      ["pending", "running"].includes(String(job.status)),
    ) ?? false;
  const selectedSearch = project.data?.searches.find(
    (search) => String(search.id) === params.get("run_id"),
  );
  const latestJob = project.data?.jobs.find(
    (job) => !selectedSearch || job.run_id === selectedSearch.id,
  );
  const jobResult = latestJob?.result_json
    ? (JSON.parse(String(latestJob.result_json)) as {
        imported: number; added?: number; existing?: number; exhausted: boolean;
      })
    : null;
  const importRevision = project.data?.jobs
    .map((job) => `${job.id}:${job.status}:${job.updated_at}`)
    .join("|");
  const candidates = useQuery({
    queryKey: ["candidates", projectId, requestParams],
    queryFn: () =>
      api<CandidatePage>(`/projects/${projectId}/candidates?${requestParams}`),
    refetchInterval: activeImport ? 2000 : false,
  });
  // Duplicate-only imports update provenance without changing the candidate count.
  // Invalidate on job transitions too, including completion between polling ticks.
  useEffect(() => {
    void client.invalidateQueries({ queryKey: ["candidates", projectId] });
  }, [client, projectId, project.data?.counts.total, importRevision]);
  const items = candidates.data?.items ?? [];
  const focusedPost =
    items.find((item) => item.post_id === focused) ?? items[0];
  const restorationKey = `studio-scroll:${projectId}:${requestParams}`;
  useLayoutEffect(() => {
    if (!scroll.current) return;
    const observer = new ResizeObserver(([entry]) =>
      setColumns(Math.max(2, Math.floor(entry.contentRect.width / 180))),
    );
    observer.observe(scroll.current);
    return () => observer.disconnect();
  }, []);
  const virtual = useVirtualizer({
    count: Math.ceil(items.length / columns),
    getScrollElement: () => scroll.current,
    estimateSize: () => 256,
    overscan: 1,
  });
  const rows = virtual.getVirtualItems();
  const visibleKey = rows.map((row) => row.index).join(",");
  useEffect(() => {
    const ids = rows.flatMap((row) =>
      items
        .slice(row.index * columns, (row.index + 1) * columns)
        .map((item) => item.post_id),
    );
    if (ids.length)
      void api("/media/prefetch", {
        method: "POST",
        body: JSON.stringify({ post_ids: ids.slice(0, 64) }),
      }).catch(() => {});
  }, [visibleKey, candidates.data, columns]);
  useEffect(() => {
    setSelected(new Set());
    const saved = sessionStorage.getItem(restorationKey);
    if (saved && scroll.current) {
      const value = JSON.parse(saved) as {
        offset: number;
        focused: number | null;
      };
      scroll.current.scrollTop = value.offset;
      setFocused(value.focused);
    } else if (scroll.current) scroll.current.scrollTop = 0;
  }, [restorationKey, candidates.isSuccess]);
  const persist = useCallback(
    () =>
      sessionStorage.setItem(
        restorationKey,
        JSON.stringify({ offset: scroll.current?.scrollTop ?? 0, focused }),
      ),
    [restorationKey, focused],
  );
  // Persist on scroll/open: an unmount effect sees a cleared DOM ref and would overwrite the offset with zero.
  function openPost(post: Candidate) {
    persist();
    navigate(`/projects/${projectId}/posts/${post.post_id}`, {
      state: {
        ids: items.map((item) => item.post_id),
        gallery: `/projects/${projectId}?${requestParams}`,
      },
    });
  }
  function move(delta: number) {
    const index = Math.max(
      0,
      items.findIndex((item) => item.post_id === focusedPost?.post_id),
    );
    const next = Math.max(0, Math.min(items.length - 1, index + delta));
    setFocused(items[next]?.post_id ?? null);
    virtual.scrollToIndex(Math.floor(next / columns), { align: "auto" });
  }
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (
        (event.target as HTMLElement).closest(
          "input,textarea,select,[contenteditable='true']",
        ) ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey
      )
        return;
      if (!focusedPost) return;
      const state = (
        { "1": "positive", "2": "negative", "3": "unsure" } as const
      )[event.key as "1"];
      if (state) {
        event.preventDefault();
        void reviews.edit(projectId, [focusedPost], state).then((ok) => {
          if (ok) move(1);
        });
      } else if (event.key.toLowerCase() === "z") {
        event.preventDefault();
        void reviews.undoLast(projectId, focusedPost).then((postId) => {
          if (postId && items.some((item) => item.post_id === postId))
            setFocused(postId);
        });
      } else if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        move(event.key === "ArrowRight" ? 1 : -1);
      } else if (
        event.key === "Enter" &&
        !(event.target as HTMLElement).closest("a,button")
      )
        openPost(focusedPost);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  });
  function filter(key: string, value: string) {
    const updated = new URLSearchParams(params);
    value ? updated.set(key, value) : updated.delete(key);
    updated.delete("cursor");
    setParams(updated);
  }
  async function importPosts(query = "") {
    const payload = {
      request_id: mutationId(),
      query,
      source_kind: query ? "query" : "target",
    };
    const run = async () => {
      setImporting(true);
      setImportError("");
      try {
        const result = await api<{ run_id?: number; job_id: number | null; exhausted: boolean; query: string }>(`/projects/${projectId}/discovery`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        // Show the requested search, clearing old review filters/cursors. The full
        // collection and its labels remain available through All imported posts.
        if (result.run_id) setParams({ run_id: String(result.run_id) });
        setImportNotice(result.exhausted
          ? `No more results for “${result.query}”. All previously imported results are shown.`
          : `Search queued: “${result.query}”. You can keep reviewing.`);
        await client.invalidateQueries({ queryKey: ["project", projectId] });
        await client.invalidateQueries({ queryKey: ["candidates", projectId] });
        setRetryImport(null);
      } catch (error) {
        setImportError(errorText(error));
        setRetryImport(() => run);
      } finally {
        setImporting(false);
      }
    };
    await run();
  }
  async function batch(state: LabelState) {
    const posts = items.filter((item) => selected.has(item.post_id));
    if (posts.length && (await reviews.edit(projectId, posts, state)))
      setSelected(new Set());
  }
  const counts = project.data?.counts;
  return (
    <div className="workspace">
      <aside className="sidebar">
        <Link to="/">← All projects</Link>
        <h2>{project.data?.display_name.replaceAll("_", " ") ?? "Project"}</h2>
        <p className="muted">
          {project.data?.target_kind === "custom"
            ? "Custom target"
            : "e621 target"}{" "}
          · {counts?.total ?? 0} candidates
        </p>
        <label>
          Search results
          <select
            aria-label="Search results"
            value={params.get("run_id") ?? ""}
            onChange={(event) => setParams(event.target.value ? { run_id: event.target.value } : {})}
          >
            <option value="">All imported posts</option>
            {project.data?.searches.map((search) => (
              <option key={String(search.id)} value={String(search.id)}>{String(search.query)}</option>
            ))}
          </select>
        </label>
        <p className="muted">Project-wide review totals · buttons filter the current search</p>
        <div className="count-grid">
          {(["positive", "negative", "unsure", "unreviewed"] as const).map(
            (state) => (
              <button
                key={state}
                className={state}
                onClick={() => filter("state", state)}
              >
                <strong>{counts?.[state] ?? 0}</strong>
                {state}
              </button>
            ),
          )}
        </div>
        <details
          className="filters"
          open={window.matchMedia("(min-width: 761px)").matches}
        >
          <summary>Review filters</summary>
          <div className="filter-fields">
            <label>
              Label state
              <select
                value={params.get("state") ?? "all"}
                onChange={(e) => filter("state", e.target.value)}
              >
                {[
                  "all",
                  "unreviewed",
                  "reviewed",
                  "positive",
                  "negative",
                  "unsure",
                ].map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </label>
            <label>
              Rating
              <select
                value={params.get("rating") ?? ""}
                onChange={(e) => filter("rating", e.target.value)}
              >
                <option value="">All ratings</option>
                <option value="s">Safe</option>
                <option value="q">Questionable</option>
                <option value="e">Explicit</option>
              </select>
            </label>
            <label>
              Source
              <select
                value={params.get("source") ?? ""}
                onChange={(e) => filter("source", e.target.value)}
              >
                <option value="">All sources</option>
                {["target", "query", "seed"].map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </label>
            <label>
              Minimum score
              <input
                type="number"
                value={params.get("min_score") ?? ""}
                onChange={(e) => filter("min_score", e.target.value)}
              />
            </label>
            <label>
              From date
              <input
                type="date"
                value={params.get("after") ?? ""}
                onChange={(e) => filter("after", e.target.value)}
              />
            </label>
            <label>
              Through date
              <input
                type="date"
                value={params.get("before") ?? ""}
                onChange={(e) => filter("before", e.target.value)}
              />
            </label>
          </div>
        </details>
        <label className="check">
          <input
            type="checkbox"
            checked={hideTags}
            onChange={(e) => {
              setHideTags(e.target.checked);
              localStorage.setItem(
                "studio-hide-tags",
                String(e.target.checked),
              );
            }}
          />
          Hide tags
        </label>
        <details className="sidebar-tags">
          <summary>Focused post tags</summary>
          {focusedPost && (
            <TagSidebar tags={focusedPost.tags} hidden={hideTags} />
          )}
        </details>
      </aside>
      <main className="main-content gallery-content">
        <div className="page-heading">
          <div>
            <p className="eyebrow">CANDIDATE REVIEW</p>
            <h1>
              Posts <small>{candidates.data?.total ?? 0} matching</small>
            </h1>
          </div>
          <button
            className="primary"
            disabled={
              importing ||
              activeImport ||
              !(selectedSearch?.query ?? project.data?.query) ||
              selectedSearch?.source_kind === "seed" ||
              (selectedSearch ? !!selectedSearch.exhausted : project.data?.searches.some(
                (s) => s.query === project.data?.query && !!s.exhausted,
              ))
            }
            onClick={() => {
              void importPosts(selectedSearch ? String(selectedSearch.query) : "");
            }}
          >
            Add next 50
          </button>
        </div>
        <div className="import-status" aria-live="polite">
          {importing ? "Submitting search…" : activeImport
            ? "Importing metadata… You can keep reviewing."
            : jobResult
              ? `${jobResult.imported} matches processed · ${jobResult.added ?? jobResult.imported} new · ${jobResult.existing ?? 0} already in project.${jobResult.exhausted ? " No more results for this search." : " Add next 50 to continue."}`
              : importNotice || "1 positive · 2 negative · 3 unsure · arrows navigate · Z undo"}
          {latestJob?.status === "failed" && (
            <p className="error" role="alert">
              Search “{String(latestJob.query)}” failed: {String(latestJob.error)}. Submit this query again to retry.
            </p>
          )}
        </div>
        <form
          className="additional-search"
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            void importPosts(discoveryQuery);
          }}
        >
          <input
            aria-label="Additional discovery query"
            value={discoveryQuery}
            onChange={(e) =>
              setDiscoveryOverride({ project: projectId, query: e.target.value })
            }
            placeholder="Full e621 query, e.g. dragon score:>100"
            maxLength={500}
          />
          <button
            disabled={!discoveryQuery.trim() || importing || activeImport}
          >
            Import 50
          </button>
        </form>
        <p className="muted">
          Edit the full e621 query. Import shows this search's results; all previous posts and reviews are retained.
        </p>
        {importError && (
          <p className="error" role="alert">
            {importError}{" "}
            <button onClick={() => retryImport?.()}>Retry import</button>
          </p>
        )}
        <div className="selection-bar">
          <span>
            {selected.size
              ? `${selected.size} selected`
              : "Select posts for batch review"}
          </span>
          {selected.size > 0 && (
            <>
              {(["positive", "negative", "unsure"] as const).map((state) => (
                <button
                  key={state}
                  onClick={() => {
                    void batch(state);
                  }}
                >
                  Mark {state}
                </button>
              ))}
              <button onClick={() => setSelected(new Set())}>Clear</button>
            </>
          )}
        </div>
        {project.error && (
          <p role="alert" className="error">
            {errorText(project.error)}
          </p>
        )}
        {candidates.error && (
          <p role="alert" className="error">
            {errorText(candidates.error)}{" "}
            <button
              onClick={() => {
                void candidates.refetch();
              }}
            >
              Retry
            </button>
          </p>
        )}
        {!items.length && !candidates.isPending && (
          <div className="empty-state">
            <h2>
              {activeImport
                ? "Preparing your first posts…"
                : "No posts in this view"}
            </h2>
            <p>Import candidates or adjust the filters.</p>
          </div>
        )}
        <div
          className="gallery-scroll"
          ref={scroll}
          onScroll={persist}
          tabIndex={0}
          aria-label="Candidate gallery"
        >
          <div
            style={{
              height: virtual.getTotalSize(),
              position: "relative",
              width: "100%",
            }}
          >
            {rows.map((row) => (
              <div
                className="gallery-row"
                key={row.key}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  height: row.size,
                  transform: `translateY(${row.start}px)`,
                  gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
                }}
              >
                {items
                  .slice(row.index * columns, (row.index + 1) * columns)
                  .map((post) => (
                    <article
                      key={post.post_id}
                      className={`post-card ${post.state ?? ""} ${focusedPost?.post_id === post.post_id ? "focused" : ""}`}
                      onFocus={() => setFocused(post.post_id)}
                      onMouseEnter={() => setFocused(post.post_id)}
                    >
                      <div className="card-top">
                        <label className="check">
                          <input
                            aria-label={`Select post ${post.post_id}`}
                            type="checkbox"
                            checked={selected.has(post.post_id)}
                            onChange={(event) =>
                              setSelected((old) => {
                                const next = new Set(old);
                                event.target.checked
                                  ? next.add(post.post_id)
                                  : next.delete(post.post_id);
                                return next;
                              })
                            }
                          />
                          #{post.post_id}
                        </label>
                        <span>{post.media_ext?.toUpperCase()}</span>
                      </div>
                      <button
                        className="thumbnail-link"
                        aria-label={`Open post ${post.post_id}`}
                        onClick={() => openPost(post)}
                      >
                        <Thumbnail
                          id={post.post_id}
                          available={post.has_preview}
                          onLoaded={reportPaint}
                        />
                        {["webm", "mp4"].includes(post.media_ext ?? "") && (
                          <span className="video-badge">▶ Video</span>
                        )}
                      </button>
                      <div className="card-meta">
                        <span>
                          ♥ {post.score} · {post.rating?.toUpperCase()}
                        </span>
                        <span className={post.state ?? ""}>
                          {post.state ?? "unreviewed"}
                        </span>
                      </div>
                      <div className="quick-labels">
                        {(["positive", "negative", "unsure"] as const).map(
                          (state, i) => (
                            <button
                              key={state}
                              className={state}
                              aria-label={`Mark post ${post.post_id} ${state}`}
                              disabled={
                                !!reviews.pending[
                                  `${projectId}:${post.post_id}`
                                ]
                              }
                              onClick={() => {
                                void reviews.edit(projectId, [post], state);
                              }}
                            >
                              {["+", "−", "?"][i]}
                            </button>
                          ),
                        )}
                      </div>
                    </article>
                  ))}
              </div>
            ))}
          </div>
        </div>
        <div className="pagination">
          <button
            disabled={!params.has("cursor")}
            onClick={() => {
              const updated = new URLSearchParams(params);
              updated.delete("cursor");
              setParams(updated);
            }}
          >
            ← Newest page
          </button>
          <span>{items.length} posts on this page</span>
          <button
            disabled={!candidates.data?.next_cursor}
            onClick={() => {
              const updated = new URLSearchParams(params);
              updated.set("cursor", candidates.data!.next_cursor!);
              setParams(updated);
            }}
          >
            Older page →
          </button>
        </div>
        {focusedPost && (
          <div className="focused-review">
            <span>Focused: #{focusedPost.post_id}</span>
            <ReviewButtons project={projectId} post={focusedPost} />
          </div>
        )}
      </main>
    </div>
  );
}
