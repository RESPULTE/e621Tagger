import { StudioIcon } from "../components/StudioIcon";
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
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
  type Candidate,
  type CandidatePage,
  type Project as ProjectInfo,
} from "../api/client";
import { useReviews } from "../components/Reviews";
import { Thumbnail } from "../components/Thumbnail";
import { SaveThumbnail } from "../components/FavoriteButton";
import { QueryInput } from "../components/QueryInput";
import {
  SimilarityFolders,
  useSimilarity,
  similarityCandidates,
} from "../components/Similarity";
import { Downloads } from "./Downloads";

const scrollPositions = new Map<string, { offset: number; expires: number }>();
let scrollGeneration = 0;
for (const event of ["studio-session-expired", "studio-signed-out"])
  window.addEventListener(event, () => {
    scrollPositions.clear();
    scrollGeneration++;
  });
// Reuse the measured layout on remount so a narrow screen does not briefly
// mount four columns of uncached previews before ResizeObserver runs.
let lastGalleryColumns = 4;
function savedOffset(key: string) {
  const entry = scrollPositions.get(key);
  return entry && entry.expires > Date.now() ? entry.offset : 0;
}

export function Project({ canReview }: { canReview: boolean }) {
  const projectId = Number(useParams().id);
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const reviews = useReviews();
  const client = useQueryClient();
  const scroll = useRef<HTMLDivElement>(null);
  const [columns, setColumns] = useState(() => lastGalleryColumns);
  const [focused, setFocused] = useState<number | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
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
  const labelFilter = tab === "tagged" && ["positive", "negative"].includes(params.get("label") ?? "")
    ? params.get("label")! : "all";
  const favoriteFilter = tab === "tagged" && ["favourited", "unfavourited"].includes(params.get("favorite") ?? "")
    ? params.get("favorite")! : "all";
  function cycleFilter(key: "label" | "favorite", values: string[], current: string) {
    const next = new URLSearchParams(params);
    const value = values[(values.indexOf(current) + 1) % values.length];
    if (value === "all") next.delete(key);
    else next.set(key, value);
    setParams(next);
    setFocused(null);
    scroll.current?.scrollTo(0, 0);
  }
  const similarityFilter = params.get("similar_q") ?? "";
  const similarity = useSimilarity(
    projectId,
    tab === "similarity",
    params.get("search"),
    similarityFilter,
  );
  const query = params.get("q") ?? project.data?.query ?? "";
  const candidates = useInfiniteQuery({
    queryKey: [
      tab === "query" ? "feed" : "candidates",
      projectId,
      query,
      tab,
      labelFilter,
      favoriteFilter,
    ],
    enabled: !!project.data && ["query", "tagged"].includes(tab),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => {
      const search = new URLSearchParams({ limit: "50" });
      if (pageParam) search.set("cursor", pageParam);
      if (tab === "query") search.set("query", query);
      else {
        search.set("state", labelFilter === "all" ? "reviewed" : labelFilter);
        if (favoriteFilter !== "all") search.set("favorite", String(favoriteFilter === "favourited"));
      }
      return api<CandidatePage>(
        `/projects/${projectId}/${tab === "query" ? "query" : "candidates"}?${search}`,
        { signal },
      );
    },
    getNextPageParam: (page) => page.next_cursor,
    maxPages: 40,
    gcTime: 5 * 60_000,
    retry: false,
  });
  const items =
    tab === "similarity"
      ? similarityCandidates(similarity.detail.error ? undefined : similarity.detail.data)
      : [
          ...new Map(
            (candidates.data?.pages.flatMap((p) => p.items) ?? []).map((p) => [
              p.post_id,
              p,
            ]),
          ).values(),
        ];
  const focusedPost = items.find((p) => p.post_id === focused) ?? items[0];
  const galleryKey = JSON.stringify([projectId, tab, query, labelFilter, favoriteFilter, params.get("search"), similarityFilter]);
  useLayoutEffect(() => {
    if (!scroll.current) return;
    const observer = new ResizeObserver(([entry]) => {
      lastGalleryColumns = Math.max(2, Math.floor(entry.contentRect.width / 180));
      setColumns(lastGalleryColumns);
    });
    observer.observe(scroll.current);
    return () => observer.disconnect();
  }, [tab]);
  const virtual = useVirtualizer({
    count: Math.ceil(items.length / columns),
    getScrollElement: () => scroll.current,
    estimateSize: () => 256,
    overscan: 1,
    initialOffset: () => savedOffset(galleryKey),
  });
  const hasItems = items.length > 0;
  useLayoutEffect(() => {
    const element = scroll.current;
    if (!element || !hasItems) return;
    virtual.scrollToOffset(savedOffset(galleryKey));
    const generation = scrollGeneration;
    const remember = () => {
      if (generation !== scrollGeneration) return;
      scrollPositions.delete(galleryKey);
      scrollPositions.set(galleryKey, { offset: element.scrollTop, expires: Date.now() + 5 * 60_000 });
      while (scrollPositions.size > 100)
        scrollPositions.delete(scrollPositions.keys().next().value!);
    };
    element.addEventListener("scroll", remember, { passive: true });
    return () => {
      remember();
      element.removeEventListener("scroll", remember);
    };
  }, [galleryKey, hasItems]);
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
    // Seed from the gallery, then revalidate independently in the viewer.
    const key = ["post", projectId, post.post_id];
    if (!client.getQueryData(key)) client.setQueryData(key, post, { updatedAt: 0 });
    const context = post.similarity
      ? `?index=${encodeURIComponent(post.similarity.index_revision)}${post.similarity.seed_post_id ? `&seed=${post.similarity.seed_post_id}` : ""}${post.similarity.search_id ? `&similar_search=${post.similarity.search_id}` : ""}`
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
        { "1": "positive", "2": "negative" } as const
      )[event.key as "1"];
      if (state && canReview) {
        event.preventDefault();
        void reviews.edit(projectId, [focusedPost], focusedPost.state === state ? null : state);
      } else if (event.key.toLowerCase() === "z" && canReview) {
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
    if ((draft ?? query).trim() === query) void candidates.refetch();
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
            {(["positive", "negative"] as const).map((state) => (
              <span className={state} key={state}>
                <StudioIcon name={state} /><span className="total-label">{state === "positive" ? "Positive" : "Negative"}</span>
                <strong
                  aria-label={`${state}: ${project.data?.counts[state] ?? 0}`}
                >
                  {project.data?.counts[state] ?? 0}
                </strong>
              </span>
            ))}
          </div>
          <Link className="export-button" to={`/projects/${projectId}/datasets`} aria-label="Dataset export" title="Dataset export"><StudioIcon name="export" /><span>Export</span></Link>
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
                  ) : value === "similarity" ? (
                    <><circle cx="10" cy="10" r="6" /><path d="m15 15 6 6M19 3v6m-3-3h6" /></>
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
          className={`gallery-panel${tab === "similarity" ? " similarity-panel" : ""}`}
        >
          {tab === "downloads" ? (
            <Downloads />
          ) : (
            <>
              {tab === "similarity" ? (
                <SimilarityFolders
                  key={`${projectId}:${similarityFilter}`}
                  project={projectId}
                  projectName={project.data?.display_name ?? "Project"}
                  state={similarity}
                  filter={similarityFilter}
                  setFilter={(value) => {
                    const next = new URLSearchParams(params);
                    if (value) next.set("similar_q", value);
                    else next.delete("similar_q");
                    setParams(next);
                    setFocused(null);
                    scroll.current?.scrollTo(0, 0);
                    if (value === similarityFilter) void similarity.detail.refetch();
                  }}
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
                  <span>{candidates.data?.pages[0]?.total ?? 0} reviewed</span>
                  <div className="tagged-filters" role="group" aria-label="Tagged image filters">
                    <button
                      className={`tagged-filter filter-${labelFilter}`}
                      aria-label={`Label filter: ${labelFilter}`}
                      title={`Labels: ${labelFilter}. Show ${labelFilter === "all" ? "positive" : labelFilter === "positive" ? "negative" : "all"} images`}
                      onClick={() => cycleFilter("label", ["all", "positive", "negative"], labelFilter)}
                    >
                      <span className="tagged-filter-icon" aria-hidden="true">
                        {labelFilter === "all" ? ">w<" : <StudioIcon name={labelFilter === "positive" ? "positive" : "negative"} />}
                      </span>
                      <span>Tag status</span>
                    </button>
                    <button
                      className={`tagged-filter filter-${favoriteFilter}`}
                      aria-label={`Favourite filter: ${favoriteFilter}`}
                      title={`Favourites: ${favoriteFilter}. Show ${favoriteFilter === "all" ? "favourited" : favoriteFilter === "favourited" ? "unfavourited" : "all"} images`}
                      onClick={() => cycleFilter("favorite", ["all", "favourited", "unfavourited"], favoriteFilter)}
                    >
                      <span className="tagged-filter-icon" aria-hidden="true">
                        {favoriteFilter === "all" ? ">w<" : <StudioIcon name="paw" />}
                      </span>
                      <span>Favourite</span>
                    </button>
                  </div>
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
                        ? labelFilter !== "all" || favoriteFilter !== "all" ? "No matching reviewed posts" : "No reviewed posts yet"
                        : "No matching posts"}
                    </h2>
                    <p>
                      {tab === "tagged"
                        ? labelFilter !== "all" || favoriteFilter !== "all" ? "Try another label or favourite filter." : "Review posts in Query to keep them here."
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
                          const summary = post.sample_summary;
                          const sampleBorder = summary && summary.total > 0
                            ? summary.positive > summary.negative ? "sample-majority-positive"
                              : summary.negative > summary.positive ? "sample-majority-negative" : ""
                            : "";
                          return (
                            <article
                              key={post.post_id}
                              className={`post-card ${state ?? ""} ${sampleBorder} ${focusedPost?.post_id === post.post_id ? "focused" : ""}`}
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
                              {canReview && <div className="quick-labels">
                                {(
                                  ["positive", "negative"] as const
                                ).map((value) => (
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
                                        state === value ? null : value,
                                      )
                                    }
                                  >
                                    <StudioIcon name={value} />
                                  </button>
                                ))}
                              </div>}
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
