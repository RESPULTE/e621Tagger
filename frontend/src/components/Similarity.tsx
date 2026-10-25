import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, errorText, mutationId, type Candidate } from "../api/client";
import type { components } from "../api/schema";
import { TaskProgress } from "./TaskProgress";

export type SimilarSearch = components["schemas"]["SimilaritySearch"];
type SimilarList = components["schemas"]["SimilarityList"];
export const activeSearch = (status: string) =>
  ["pending", "running"].includes(status);

export function useFindSimilar(project: number, post: number) {
  const client = useQueryClient();
  const [notice, setNotice] = useState("");
  const [result, setResult] = useState<string | null>(null);
  async function find() {
    setNotice("Queuing similarity search…");
    setResult(null);
    try {
      const search = await api<SimilarSearch>(`/projects/${project}/similar`, {
        method: "POST",
        body: JSON.stringify({
          post_id: post,
          limit: 32,
          request_id: mutationId(),
        }),
      });
      setNotice(`Post #${post}: ${search.stage.toLowerCase()}.`);
      setResult(search.id);
      void client.invalidateQueries({ queryKey: ["similarity-list", project] });
    } catch (error) {
      setNotice(errorText(error));
    }
  }
  return {
    find,
    notice: notice ? (
      <span className="similarity-notice" role="status">
        {notice}{" "}
        {result && (
          <Link to={`/projects/${project}?tab=similarity&search=${result}`}>
            Open results
          </Link>
        )}
      </span>
    ) : null,
  };
}

export function useSimilarity(
  project: number,
  enabled: boolean,
  selected: string | null,
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
    queryKey: ["similarity", project, current],
    enabled: enabled && !!current,
    queryFn: ({ signal }) =>
      api<SimilarSearch>(`/projects/${project}/similar/${current}`, { signal }),
    refetchInterval: (q) =>
      activeSearch(q.state.data?.status ?? "pending") ? 750 : false,
    retry: false,
  });
  return { list, detail, current };
}

export function SimilarityFolders({
  project,
  state,
  select,
}: {
  project: number;
  state: ReturnType<typeof useSimilarity>;
  select: (id: string) => void;
}) {
  const [error, setError] = useState("");
  const [hideTags, setHideTags] = useState(
    localStorage.getItem("studio-hide-tags") === "true",
  );
  const sourceId = state.detail.data?.source_post_id;
  const source = useQuery({
    queryKey: ["post", project, sourceId],
    enabled: !!sourceId,
    queryFn: ({ signal }) =>
      api<Candidate>(`/projects/${project}/candidates/${sourceId}`, { signal }),
  });
  const counts = new Map<string, number>();
  const ready =
    state.detail.data?.results.filter((entry) => entry.candidate) ?? [];
  for (const entry of ready)
    for (const tag of new Set(entry.candidate?.tags.general ?? [])) {
      counts.set(tag, (counts.get(tag) ?? 0) + 1);
    }
  const suggestions = [...counts]
    .filter(([, count]) => count >= 2)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 20);
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
    <div className="similarity-folders">
      <p className="muted">
        Hold a post, then release to find similar images. Shift+S works on a
        focused post. Searches are temporary; only your reviews and explicit
        saves are retained.
      </p>
      {(error || state.list.error || state.detail.error) && (
        <p role="alert" className="error">
          {error || errorText(state.list.error || state.detail.error)}
        </p>
      )}
      {state.list.data?.message && (
        <p role="status">{state.list.data.message}</p>
      )}
      <div className="similarity-folder-list" aria-label="Similarity sources">
        {state.list.data?.items.map((job) => (
          <article className="similarity-folder" key={job.id}>
            <button
              className={job.id === state.current ? "active" : ""}
              aria-pressed={job.id === state.current}
              onClick={() => select(job.id)}
            >
              Post #{job.source_post_id}
            </button>
            <span role="status">{job.stage}</span>
            <TaskProgress
              active={activeSearch(job.status)}
              label={`Post ${job.source_post_id} metadata progress`}
              value={
                job.status === "running" && job.total ? job.checked : undefined
              }
              total={job.total || undefined}
            />
            {activeSearch(job.status) && (
              <button onClick={() => void control(job.id, "cancel")}>
                Cancel
              </button>
            )}
            {["failed", "cancelled"].includes(job.status) && (
              <button disabled={!state.list.data?.available} onClick={() => void control(job.id, "retry")}>
                Retry
              </button>
            )}
            {job.error && (
              <p role="alert" className="error">
                {job.error}
              </p>
            )}
          </article>
        ))}
      </div>
      {state.detail.data && (
        <p>
          Source post #{state.detail.data.source_post_id} ·{" "}
          {state.detail.data.results.length} results · Lower distance = closer,
          not confidence
          {state.detail.data.index_revision === "synthetic" &&
            " · Synthetic demo results"}
        </p>
      )}
      {state.detail.data && (
        <details aria-label="Embedding assistance">
          <summary>Source tags and neighbor tag counts</summary>
          <label className="check">
            <input
              type="checkbox"
              checked={hideTags}
              onChange={(event) => {
                setHideTags(event.target.checked);
                localStorage.setItem(
                  "studio-hide-tags",
                  String(event.target.checked),
                );
              }}
            />{" "}
            Hide tags
          </label>
          <p>Source review: {source.data?.state ?? "unreviewed"}</p>
          {hideTags ? (
            <p>Tag suggestions are hidden for review.</p>
          ) : (
            <>
              <p>
                Source tags:{" "}
                {Object.values(source.data?.tags ?? {})
                  .flat()
                  .join(", ") || "Loading tags…"}
              </p>
              <p>
                {ready.length} neighbors have available local metadata. Counts
                describe source tags, not predictions.
              </p>
              <ul>
                {suggestions.map(([tag, count]) => (
                  <li key={tag}>
                    {tag} · {count}/{ready.length}
                  </li>
                ))}
              </ul>
            </>
          )}
        </details>
      )}
    </div>
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
