import { artistName } from "./artworkName";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, errorText, mutationId, type Candidate } from "../api/client";
import type { components } from "../api/schema";
import { TaskProgress } from "./TaskProgress";
import { QueryInput } from "./QueryInput";

export type SimilarSearch = components["schemas"]["SimilaritySearch"];
type SimilarList = components["schemas"]["SimilarityList"];
export const activeSearch = (status: string) =>
  ["pending", "running"].includes(status);

function SearchIcon({ action }: { action: "filter" | "clear" | "refresh" | "more" }) {
  const paths = {
    filter: "M4 5h16l-6 7v6l-4 2v-8z",
    clear: "m6 6 12 12M18 6 6 18",
    refresh: "M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1",
    more: "M12 4v16M4 12h16",
  };
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[action]} /></svg>;
}

function ReferenceSearch({ project, available, select }: {
  project: number; available: boolean; select: (id: string) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [post, setPost] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const client = useQueryClient();
  async function search(event: FormEvent, upload: boolean) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (upload && (!file || file.size > 16 * 1024 * 1024))
        throw new Error("Choose a still image within the server upload limit (at most 16 MiB).");
      const path = `/projects/${project}/similar`;
      const result = upload
        ? await api<SimilarSearch>(`${path}/reference?request_id=${mutationId()}`, {
            method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" },
          })
        : await api<SimilarSearch>(path, { method: "POST", body: JSON.stringify({
            post_id: Number(post), request_id: mutationId(), limit: 32,
          }) });
      await client.invalidateQueries({ queryKey: ["similarity-list", project] });
      select(result.id);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return <section className="reference-search" aria-label="Image similarity search">
    <form onSubmit={(event) => void search(event, true)}>
      <label>Reference image<input type="file" accept="image/jpeg,image/png,image/webp"
        onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
      <button disabled={!available || busy || !file}>Search reference image</button>
    </form>
    <form onSubmit={(event) => void search(event, false)}>
      <label>Source post ID<input type="number" min="1" step="1" required value={post}
        onChange={(event) => setPost(event.target.value)} /></label>
      <button disabled={!available || busy || !post}>Search post image</button>
    </form>
    {busy && <p role="status">Preparing image search…</p>}
    {error && <p role="alert" className="error">{error}</p>}
  </section>;
}

export function useFindSimilar(project: number, post: number, referenceImageUrl?: string) {
  const client = useQueryClient();
  const source = JSON.stringify([project, post, referenceImageUrl ?? null]);
  const generation = useRef({ source, invocation: 0 });
  if (generation.current.source !== source) generation.current = { source, invocation: generation.current.invocation + 1 };
  const [display, setDisplay] = useState<{ source: string; notice: string; result: string | null }>({ source, notice: "", result: null });
  useEffect(() => () => { generation.current.invocation++; }, []);
  async function find() {
    const requestSource = source;
    const requestUrl = referenceImageUrl;
    const requestProject = project;
    const requestPost = post;
    const invocation = ++generation.current.invocation;
    const show = (notice: string, result: string | null = null) => {
      if (generation.current.source === requestSource && generation.current.invocation === invocation)
        setDisplay({ source: requestSource, notice, result });
    };
    show("Checking image search availability...");
    try {
      const capability = await client.fetchQuery({
        queryKey: ["similarity-list", requestProject], staleTime: 0,
        queryFn: () => api<SimilarList>(`/projects/${requestProject}/similar`),
      });
      if (!capability.available) {
        show(capability.message ?? "Image search unavailable: GPU worker is not ready");
        return;
      }
      show("Queuing similarity search...");
      let search: SimilarSearch;
      if (requestUrl) {
        const response = await fetch(requestUrl, { credentials: "same-origin" });
        if (!response.ok) throw new Error(`Could not load reference image (${response.status}).`);
        const image = await response.blob();
        search = await api<SimilarSearch>(`/projects/${requestProject}/similar/reference?request_id=${mutationId()}`, {
          method: "POST", body: image, headers: { "Content-Type": "application/octet-stream" },
        });
      } else {
        search = await api<SimilarSearch>(`/projects/${requestProject}/similar`, {
          method: "POST", body: JSON.stringify({ post_id: requestPost, limit: 32, request_id: mutationId() }),
        });
      }
      show(`Similarity search: ${search.stage.toLowerCase()}.`, search.id);
      void client.invalidateQueries({ queryKey: ["similarity-list", requestProject] });
    } catch (error) {
      show(errorText(error));
    }
  }
  const { notice, result } = display.source === source ? display : { notice: "", result: null };
  return {
    find,
    notice: notice ? (
      <span className="similarity-notice" role="status">
        {notice} {result && <Link to={`/projects/${project}?tab=similarity&search=${result}`}>Open results</Link>}
      </span>
    ) : null,
  };
}

export function useSimilarity(
  project: number,
  enabled: boolean,
  selected: string | null,
  filter = "",
) {
  const list = useQuery({
    queryKey: ["similarity-list", project],
    enabled,
    queryFn: ({ signal }) =>
      api<SimilarList>(`/projects/${project}/similar`, { signal }),
    refetchInterval: enabled ? 1000 : false,
  });
  const current = selected ?? list.data?.items.at(-1)?.id;
  const detail = useQuery({
    queryKey: ["similarity", project, current, filter],
    enabled: enabled && !!current,
    queryFn: ({ signal }) =>
      api<SimilarSearch>(`/projects/${project}/similar/${current}?${new URLSearchParams({ query: filter })}`, { signal }),
    refetchInterval: (q) =>
      activeSearch(q.state.data?.status ?? "pending") || list.data?.items.some((item) => activeSearch(item.status)) ? 750 : false,
    retry: false,
  });
  return { list, detail, current };
}

export function SimilarityFolders({
  project,
  projectName,
  state,
  select,
  filter,
  setFilter,
}: {
  project: number;
  projectName: string;
  state: ReturnType<typeof useSimilarity>;
  select: (id: string) => void;
  filter: string;
  setFilter: (query: string) => void;
}) {
  const [error, setError] = useState("");
  const [draft, setDraft] = useState(filter);
  const sourceId = state.detail.data?.source_post_id;
  const searches = state.list.data?.items ?? [];
  const history = searches.slice(-20).reverse();
  const ready = state.detail.data?.results.filter((entry) => entry.candidate) ?? [];
  function searchName(job: SimilarSearch) {
    const artist = job.query_type === "upload" ? "Uploaded reference" : artistName(job.source_artists);
    const siblings = searches.filter((other) =>
      (other.query_type === "upload" ? "Uploaded reference" : artistName(other.source_artists)) === artist);
    const sources = [...new Set(siblings.map((other) => other.source_post_id ?? other.query_sha256 ?? other.id))];
    const position = sources.indexOf(job.source_post_id ?? job.query_sha256 ?? job.id);
    return `${projectName.replaceAll("_", " ")} · ${artist}${sources.length > 1 && position >= 0 ? ` · ${position + 1}` : ""}`;
  }
  const client = useQueryClient();
  async function control(id: string, action: string) {
    try {
      setError("");
      await api(`/projects/${project}/similar/${id}/${action}`, {
        method: "POST",
        body: JSON.stringify({ request_id: mutationId() }),
      });
      await Promise.all([
        client.invalidateQueries({ queryKey: ["similarity-list", project] }),
        client.invalidateQueries({ queryKey: ["similarity", project, id] }),
      ]);
    } catch (e) {
      setError(errorText(e));
    }
  }
  return (
    <aside className="similarity-folders" aria-label="Image search controls">
      <form className="similarity-filter" onSubmit={(event) => {
        event.preventDefault();
        setFilter(draft.trim());
      }}>
        <QueryInput label="Filter similar images" value={draft} onChange={setDraft}
          placeholder="Filter matches, e.g. dragon -comic rating:s" />
        <button className="primary" aria-label="Apply filter" title="Apply filter"><SearchIcon action="filter" />Filter</button>
        {(draft || filter) && <button type="button" className="similarity-icon-button" aria-label="Clear filter" title="Clear filter" onClick={() => {
          setDraft("");
          setFilter("");
        }}><SearchIcon action="clear" /></button>}
        {state.detail.isFetching && filter && <p role="status">Filtering matches…</p>}
        {filter && state.detail.data && !state.detail.isFetching && !state.detail.error && !ready.length &&
          <p role="status">No loaded matches fit this query.</p>}
      </form>
      {state.detail.data && (
        <div className="selected-image">
          {sourceId && <img className="similarity-source-preview" src={`/api/media/${sourceId}/preview`} alt={`Source post ${sourceId}`} />}
          <p><strong>{searchName(state.detail.data)}</strong>
          <span className="muted"> · {filter ? `${state.detail.data.results.length} of ${state.detail.data.unfiltered_count} loaded matches` : `${state.detail.data.results.length} results`}</span>
          {state.detail.data.index_revision === "synthetic" && <span className="muted"> · Demo</span>}
          </p>
        </div>
      )}
      <details className="reference-tools">
        <summary aria-label="Search another image" title="Search another image"><SearchIcon action="more" /></summary>
        <ReferenceSearch project={project} available={!!state.list.data?.available} select={select} />
      </details>
      {(error || state.list.error || state.detail.error) && (
        <p role="alert" className="error">
          {error || errorText(state.list.error || state.detail.error)}
        </p>
      )}
      {state.list.data?.message && (
        <p role="status">{state.list.data.message}</p>
      )}
      <details className="similarity-history" open>
      <summary>Recent searches <span className="muted">{history.length}</span></summary>
      <div className="similarity-folder-list" aria-label="Similarity sources">
        {!history.length && <p className="muted">No recent searches</p>}
        {history.map((job) => (
          <article className={`similarity-folder${job.id === state.current ? " selected" : ""}`} key={job.id} data-source-id={job.source_post_id ?? "upload"}>
            <button
              className={job.id === state.current ? "active" : ""}
              aria-pressed={job.id === state.current}
              title={searchName(job)}
              onClick={() => select(job.id)}
            >
              {searchName(job)}
            </button>
            <span className="similarity-row-status" role="status">{job.stage}</span>
            <span className="similarity-row-count">{job.unfiltered_count} matches</span>
            <TaskProgress
              active={activeSearch(job.status)}
              label={`Post ${job.source_post_id} metadata progress`}
              value={
                job.status === "running" && job.total ? job.checked : undefined
              }
              total={job.total || undefined}
            />
            {activeSearch(job.status) && (
              <button className="similarity-icon-button" aria-label="Cancel" title="Cancel search" onClick={() => void control(job.id, "cancel")}>
                <SearchIcon action="clear" />
              </button>
            )}
            {["failed", "cancelled"].includes(job.status) && (
              <button className="similarity-icon-button" aria-label="Retry" title="Retry search" disabled={!state.list.data?.available} onClick={() => void control(job.id, "retry")}>
                <SearchIcon action="refresh" />
              </button>
            )}
            {job.status === "done" && (
              <>
                {job.has_more && <button className="similarity-icon-button" aria-label="Load more matches" title="Load more matches" onClick={() => { select(job.id); void control(job.id, "more"); }}>
                  <SearchIcon action="more" />
                </button>}
                <button className="similarity-icon-button" aria-label="Refresh search" disabled={!state.list.data?.available} title="Refresh search"
                  onClick={() => void control(job.id, "retry")}>
                  <SearchIcon action="refresh" />
                </button>
              </>
            )}
            {job.error && (
              <p role="alert" className="error">
                {job.error}
              </p>
            )}
          </article>
        ))}
      </div>
      </details>
      {state.detail.data?.status === "done" && !state.detail.data.has_more && (
        <p className="muted search-exhausted" role="status">No more matches in this result set.</p>
      )}
    </aside>
  );
}

export function similarityCandidates(search?: SimilarSearch): Candidate[] {
  return (
    search?.results.map(
      (result) =>
        result.candidate ?? {
          conflict: false,
          post_id: result.post_id,
          rating: null,
          md5: null,
          tags: {},
          score: 0,
          posted_at: null,
          media_ext: null,
          width: null,
          height: null,
          unavailable: false,
          has_preview: false,
          has_sample: false,
          has_original: false,
          has_video_fallback: false,
          state: null,
          version: 0,
          event_id: null,
          sources: [],
          favorite: {
            post_id: result.post_id,
            version: 0,
            wanted: false,
            present: false,
            status: "idle",
            error: null,
            updated_at: 0,
          },
        },
    ) ?? []
  );
}
