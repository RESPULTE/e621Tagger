import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
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
  type Project as ProjectInfo,
} from "../api/client";
import { useReviews } from "../components/Reviews";
import { Thumbnail } from "../components/Thumbnail";
import { FavoriteButton, SaveThumbnail } from "../components/FavoriteButton";
import { QueryInput } from "../components/QueryInput";
import {
  SimilarityFolders,
  useSimilarity,
  similarityCandidates,
} from "../components/Similarity";
import { Downloads } from "./Downloads";

export function Project() {
  const projectId = Number(useParams().id);
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const reviews = useReviews();
  const scroll = useRef<HTMLDivElement>(null);
  const [columns, setColumns] = useState(4);
  const [focused, setFocused] = useState<number | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [visit] = useState(mutationId);
  const [revision, setRevision] = useState(0);
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api<ProjectInfo>(`/projects/${projectId}`),
    refetchInterval: 2000,
  });
  const tab = ["query", "tagged", "similarity", "downloads"].includes(
    params.get("tab") ?? "",
  )
    ? params.get("tab")!
    : "query";
  const similarity = useSimilarity(
    projectId,
    tab === "similarity",
    params.get("search"),
  );
  const query = params.get("q") ?? project.data?.query ?? "";
  const candidates = useInfiniteQuery({
    queryKey: [
      tab === "query" ? "feed" : "candidates",
      projectId,
      query,
      tab,
      visit,
      revision,
    ],
    enabled: !!project.data && ["query", "tagged"].includes(tab),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => {
      const search = new URLSearchParams({ limit: "50" });
      if (pageParam) search.set("cursor", pageParam);
      if (tab === "query") search.set("query", query);
      else search.set("state", "reviewed");
      return api<CandidatePage>(
        `/projects/${projectId}/${tab === "query" ? "query" : "candidates"}?${search}`,
        { signal },
      );
    },
    getNextPageParam: (page) => page.next_cursor,
    maxPages: 40,
    gcTime: 0,
    retry: false,
  });
  const items =
    tab === "similarity"
      ? similarityCandidates(similarity.detail.data)
      : [
          ...new Map(
            (candidates.data?.pages.flatMap((p) => p.items) ?? []).map((p) => [
              p.post_id,
              p,
            ]),
          ).values(),
        ];
  const focusedPost = items.find((p) => p.post_id === focused) ?? items[0];
  useLayoutEffect(() => {
    if (!scroll.current) return;
    const observer = new ResizeObserver(([entry]) =>
      setColumns(Math.max(2, Math.floor(entry.contentRect.width / 180))),
    );
    observer.observe(scroll.current);
    return () => observer.disconnect();
  }, [tab]);
  const virtual = useVirtualizer({
    count: Math.ceil(items.length / columns),
    getScrollElement: () => scroll.current,
    estimateSize: () => 256,
    overscan: 1,
  });
  const rows = virtual.getVirtualItems();
  const nearEnd =
    !items.length ||
    (rows.at(-1)?.index ?? 0) >= Math.ceil(items.length / columns) - 3;
  useEffect(() => {
    if (
      ["query", "tagged"].includes(tab) &&
      nearEnd &&
      candidates.hasNextPage &&
      !candidates.isFetching &&
      !candidates.isError
    )
      void candidates.fetchNextPage();
  }, [
    nearEnd,
    candidates.hasNextPage,
    candidates.isFetching,
    candidates.data,
    tab,
  ]);
  const first = items[0]?.post_id;
  const previous = useRef<number | undefined>(undefined);
  useLayoutEffect(() => {
    if (
      previous.current &&
      first &&
      first !== previous.current &&
      candidates.data?.pages.length === 40 &&
      scroll.current
    )
      scroll.current.scrollTop = Math.max(
        0,
        scroll.current.scrollTop - Math.ceil(50 / columns) * 256,
      );
    previous.current = first;
  }, [first]);
  function openPost(post: Candidate) {
    const context = post.similarity
      ? `?seed=${post.similarity.seed_post_id}&index=${encodeURIComponent(post.similarity.index_revision)}`
      : "";
    navigate(`/projects/${projectId}/posts/${post.post_id}${context}`, {
      state: {
        ids: items.map((p) => p.post_id),
        gallery: `/projects/${projectId}?${params}`,
        similarity: post.similarity,
      },
    });
  }
  function switchTab(value: string) {
    setParams({
      ...(params.get("q") ? { q: params.get("q")! } : {}),
      ...(value !== "query" ? { tab: value } : {}),
    });
    setFocused(null);
  }
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (
        tab === "downloads" ||
        (event.target as HTMLElement).closest(
          "input,textarea,select,button,a,[contenteditable='true']",
        ) ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        !focusedPost ||
        (tab === "similarity" &&
          !similarity.detail.data?.results.find(
            (r) => r.post_id === focusedPost.post_id,
          )?.candidate)
      )
        return;
      const state = (
        { "1": "positive", "2": "negative", "3": "unsure" } as const
      )[event.key as "1"];
      if (state) {
        event.preventDefault();
        void reviews.edit(projectId, [focusedPost], state);
      } else if (event.key.toLowerCase() === "z") {
        event.preventDefault();
        void reviews.undoLast(projectId, focusedPost);
      } else if (event.key === "Enter") {
        event.preventDefault();
        openPost(focusedPost);
      } else if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        const index = Math.max(
          0,
          items.findIndex((p) => p.post_id === focusedPost.post_id),
        );
        const next = Math.max(
          0,
          Math.min(
            items.length - 1,
            index + (event.key === "ArrowRight" ? 1 : -1),
          ),
        );
        setFocused(items[next]?.post_id ?? null);
        virtual.scrollToIndex(Math.floor(next / columns), { align: "auto" });
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  });
  function search(event: FormEvent) {
    event.preventDefault();
    setParams({ q: (draft ?? query).trim() });
    setRevision((n) => n + 1);
    if (scroll.current) scroll.current.scrollTop = 0;
  }
  return (
    <div className="workspace gallery-layout">
      <main className="main-content gallery-content">
        <div className="project-heading">
          <h1>
            {project.data?.display_name.replaceAll("_", " ") ?? "Project"}
          </h1>
          <div className="review-totals" aria-label="Review totals">
            {(["positive", "negative", "unsure"] as const).map((state, i) => (
              <span className={state} key={state}>
                <span className="total-label">{["+", "−", "?"][i]}</span>
                <strong
                  aria-label={`${state}: ${project.data?.counts[state] ?? 0}`}
                >
                  {project.data?.counts[state] ?? 0}
                </strong>
              </span>
            ))}
          </div>
        </div>
        <nav className="project-tabs" role="tablist" aria-label="Project views">
          {(["query", "tagged", "similarity", "downloads"] as const).map(
            (value, i) => (
              <button
                key={value}
                role="tab"
                aria-selected={tab === value}
                aria-controls={`panel-${value}`}
                id={`tab-${value}`}
                tabIndex={tab === value ? 0 : -1}
                className={tab === value ? "active" : ""}
                onClick={() => switchTab(value)}
                onKeyDown={(event) => {
                  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
                    event.preventDefault();
                    const next = (i + (event.key === "ArrowRight" ? 1 : 3)) % 4;
                    switchTab(
                      ["query", "tagged", "similarity", "downloads"][next],
                    );
                    document
                      .getElementById(
                        `tab-${["query", "tagged", "similarity", "downloads"][next]}`,
                      )
                      ?.focus();
                  }
                }}
              >
                <svg
                  viewBox="0 0 24 24"
                  width="16"
                  height="16"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  aria-hidden="true"
                >
                  {value === "query" ? (
                    <>
                      <circle cx="10" cy="10" r="6" />
                      <path d="m15 15 6 6" />
                    </>
                  ) : value === "tagged" ? (
                    <path d="M3 3h9l9 9-9 9-9-9V3Zm5 5h.01" />
                  ) : (
                    <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />
                  )}
                </svg>
                {value === "query"
                  ? "Query"
                  : value === "tagged"
                    ? "Tagged"
                    : value === "similarity"
                      ? "Similarity"
                      : "Downloads"}
              </button>
            ),
          )}
        </nav>
        {project.error && (
          <p className="error" role="alert">
            {errorText(project.error)}
          </p>
        )}
        {!!project.data?.duplicate_projects?.length && (
          <p className="compact-notice">
            Existing reviews from {project.data.duplicate_projects.length + 1}{" "}
            project records are shown together.{" "}
            {project.data.conflicts
              ? `${project.data.conflicts} conflicting posts require explicit resolution and are excluded from exports.`
              : "Original review history is preserved."}
          </p>
        )}
        <section
          id={`panel-${tab}`}
          role="tabpanel"
          aria-labelledby={`tab-${tab}`}
          className="gallery-panel"
        >
          {tab === "downloads" ? (
            <Downloads />
          ) : (
            <>
              {tab === "similarity" ? (
                <SimilarityFolders
                  project={projectId}
                  state={similarity}
                  select={(id) => {
                    const next = new URLSearchParams(params);
                    next.set("search", id);
                    setParams(next);
                    scroll.current?.scrollTo(0, 0);
                  }}
                />
              ) : tab === "query" ? (
                <form className="additional-search" onSubmit={search}>
                  <QueryInput
                    label="Discovery query"
                    value={draft ?? query}
                    onChange={setDraft}
                    placeholder="Search e621…"
                  />
                  <button disabled={!(draft ?? query).trim()}>Search</button>
                </form>
              ) : (
                <div className="tagged-actions">
                  <span>{project.data?.counts.total ?? 0} reviewed</span>
                  <Link to={`/projects/${projectId}/datasets`}>
                    Dataset & export
                  </Link>
                  <span className="muted">
                    Review target: {project.data?.display_name}
                  </span>
                </div>
              )}
              {tab !== "similarity" && candidates.error && (
                <p className="error" role="alert">
                  {errorText(candidates.error)}{" "}
                  <button onClick={() => void candidates.refetch()}>
                    Retry
                  </button>
                </p>
              )}
              {tab !== "similarity" &&
                !items.length &&
                !candidates.isPending &&
                !candidates.isFetching && (
                  <div className="empty-state">
                    <h2>
                      {tab === "tagged"
                        ? "No reviewed posts yet"
                        : "No matching posts"}
                    </h2>
                    <p>
                      {tab === "tagged"
                        ? "Review posts in Query to keep them here."
                        : "Try another discovery query."}
                    </p>
                  </div>
                )}
              <div
                className="gallery-scroll"
                ref={scroll}
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
                        gridTemplateColumns: `repeat(${columns},minmax(0,1fr))`,
                      }}
                    >
                      {items
                        .slice(row.index * columns, (row.index + 1) * columns)
                        .map((post) => {
                          const neighbor =
                            tab === "similarity"
                              ? similarity.detail.data?.results.find(
                                  (r) => r.post_id === post.post_id,
                                )
                              : undefined;
                          if (neighbor && neighbor.metadata_state !== "ready")
                            return (
                              <article key={post.post_id} className="post-card">
                                <span className="thumbnail-placeholder">
                                  Loading post #{post.post_id}…
                                </span>
                                <span>
                                  #{neighbor.rank} ·{" "}
                                  {neighbor.distance.toFixed(3)}
                                </span>
                              </article>
                            );
                          const pending =
                            reviews.pending[`${projectId}:${post.post_id}`];
                          const state = pending?.saving
                            ? pending.state
                            : post.state;
                          return (
                            <article
                              key={post.post_id}
                              className={`post-card ${state ?? ""} ${focusedPost?.post_id === post.post_id ? "focused" : ""}`}
                              onFocus={() => setFocused(post.post_id)}
                              onMouseEnter={() => setFocused(post.post_id)}
                            >
                              <SaveThumbnail
                                project={projectId}
                                post={post}
                                onOpen={() => openPost(post)}
                              >
                                <Thumbnail
                                  id={post.post_id}
                                  md5={post.md5}
                                  available={post.has_preview}
                                />
                                {post.media_ext && (
                                  <span className="media-type">
                                    {post.media_ext.toUpperCase()}
                                  </span>
                                )}
                              </SaveThumbnail>
                              {neighbor && (
                                <div className="similarity-rank">
                                  #{neighbor.rank} ·{" "}
                                  {neighbor.distance.toFixed(3)}
                                </div>
                              )}
                              <div className="card-meta">
                                <span>
                                  Score {post.score} ·{" "}
                                  {post.rating?.toUpperCase()}
                                </span>
                                <span className={state ?? ""}>
                                  {post.conflict
                                    ? "Conflicting reviews"
                                    : (state ?? "")}
                                </span>
                              </div>
                              <div className="quick-labels">
                                <FavoriteButton
                                  project={projectId}
                                  post={post}
                                  compact
                                />
                                {(
                                  ["positive", "negative", "unsure"] as const
                                ).map((value, i) => (
                                  <button
                                    key={value}
                                    className={value}
                                    aria-label={`Mark post ${post.post_id} ${value}`}
                                    aria-pressed={state === value}
                                    disabled={!!pending || post.conflict}
                                    onClick={() =>
                                      void reviews.edit(
                                        projectId,
                                        [post],
                                        value,
                                      )
                                    }
                                  >
                                    {["+", "−", "?"][i]}
                                  </button>
                                ))}
                                {tab === "tagged" && (
                                  <button
                                    className="clear-review"
                                    aria-label={`Clear review for post ${post.post_id}`}
                                    disabled={!!pending || post.conflict}
                                    onClick={() =>
                                      void reviews.edit(projectId, [post], null)
                                    }
                                  >
                                    ×
                                  </button>
                                )}
                              </div>
                            </article>
                          );
                        })}
                    </div>
                  ))}
                </div>
                <div className="feed-status" aria-live="polite">
                  {tab === "similarity"
                    ? (similarity.detail.data?.stage ?? "")
                    : candidates.isFetching
                      ? "Loading…"
                      : !candidates.hasNextPage && items.length
                        ? "End of results"
                        : ""}
                </div>
              </div>
            </>
          )}
        </section>
      </main>
    </div>
  );
}
