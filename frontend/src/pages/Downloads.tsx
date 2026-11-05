import { TaskProgress } from "../components/TaskProgress";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText, mutationId } from "../api/client";
import type { components } from "../api/schema";
type Saved = components["schemas"]["SaveState"];
type Download = {
  id: number;
  purpose: string;
  destination: string;
  status: string;
  attempts: number;
  error: string | null;
  progress_bytes: number;
  total_bytes: number | null;
  dataset_id: string | null;
  post_id: number;
  thumbnail_url?: string | null;
  upvotes?: number | null;
  search_tag?: string | null;
  project_id?: number | null;
  favorite?: Saved | null;
  can_delete?: boolean;
};
const inProgress = (status: string) => ["pending", "running", "queued", "downloading"].includes(status);
export function Downloads() {
  const client = useQueryClient();
  const [error, setError] = useState("");
  const [pending, setPending] = useState<Record<number, string>>({});
  const query = useQuery({
    queryKey: ["downloads"],
    queryFn: () => api<{ items: Download[]; active: number }>("/downloads"),
    refetchInterval: 750,
  });
  async function control(job: Download, action: "delete" | "cancel" | "retry") {
    if (pending[job.id]) return;
    setPending((old) => ({ ...old, [job.id]: action }));
    try {
      setError("");
      const result = await api<{ favorite?: Saved }>(`/downloads/${job.id}/${action}`, {
        method: "POST",
        body: JSON.stringify({ request_id: mutationId() }),
      });
      if (result.favorite) client.setQueryData(["favorite", job.post_id], result.favorite);
      await Promise.all([
        client.invalidateQueries({ queryKey: ["downloads"] }),
        client.invalidateQueries({ queryKey: ["favorite", job.post_id], refetchType: "all" }),
      ]);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setPending((old) => { const next = { ...old }; delete next[job.id]; return next; });
    }
  }
  return (
    <div className="downloads">
      <p className="muted">
        {query.data?.active ?? 0} active · Saved originals go to Smut; dataset exports stay with their frozen version.
      </p>
      {(error || query.error) && <p role="alert" className="error">{error || errorText(query.error)}</p>}
      {!query.data?.items.length && !query.isPending && (
        <p>No downloads yet. Use a heart to save an original, or export reviewed dataset media.</p>
      )}
      {query.data?.items.map((job) => {
        const exporting = !!job.dataset_id;
        const running = inProgress(job.status);
        const deleting = pending[job.id] === "delete" || ["removing", "deleting"].includes(job.status) || (!!job.favorite && !job.favorite.wanted && ["queued", "removing", "retrying"].includes(job.favorite.status));
        const cancelling = pending[job.id] === "cancel" || job.status === "cancelling";
        const status = deleting ? "Deleting…" : cancelling ? "Cancelling…"
          : job.status === "done" ? "Completed"
          : running ? "In progress"
          : job.status === "failed" ? "Failed"
          : job.status === "cancelled" ? "Cancelled" : job.status;
        const tone = deleting || cancelling ? "pending" : job.status === "done" ? "completed"
          : job.status === "failed" ? "failed" : running ? "pending" : "muted";
        return (
          <article className="download-row" key={job.id} aria-label={`Post ${job.post_id} ${exporting ? "dataset export" : "favourite"}`}>
            <img className="download-thumbnail" src={job.thumbnail_url ?? `/api/media/${job.post_id}/preview`} alt={`Post ${job.post_id} preview`} loading="lazy" />
            <div className="download-details">
              <div className="download-heading">
                <strong>Post #{job.post_id}</strong>
              </div>
              <div className="download-metadata">
                <span>{job.upvotes != null ? `${job.upvotes.toLocaleString()} upvotes` : "Upvotes unavailable"}</span>
                <span className="download-tag" title={job.search_tag ?? undefined}>{job.search_tag ?? "Search tag unavailable"}</span>
                {exporting && <span>Dataset export · {job.destination}</span>}
              </div>
              <div className="download-progress">
                <TaskProgress active={running && !deleting && !cancelling} value={job.progress_bytes} total={job.total_bytes ?? undefined} label={`Post ${job.post_id} download progress`} />
                <span>{(job.progress_bytes / 1048576).toFixed(1)} MiB{job.total_bytes ? ` / ${(job.total_bytes / 1048576).toFixed(1)} MiB` : ""}</span>
                {exporting && running && <button disabled={!!pending[job.id]} onClick={() => void control(job, "cancel")}>Cancel</button>}
                {["failed", "cancelled"].includes(job.status) && <button disabled={!!pending[job.id]} onClick={() => void control(job, "retry")}>Retry</button>}
                {job.status === "done" && exporting && <>
                  <a href={`/api/datasets/${job.dataset_id}/media/files/${job.post_id}`}>File</a>
                  <a href={`/api/datasets/${job.dataset_id}/media/manifest`}>Checked manifest</a>
                  <a href={`/api/datasets/${job.dataset_id}/media/bundle`}>Bundle</a>
                </>}
              </div>
              {job.error && <p className="error">{job.error}</p>}
            </div>
            <div className="download-actions">
              <span className={`download-state ${tone}`} role="status">{status}</span>
              {!exporting && (job.can_delete ?? ["done", "pending", "running", "queued", "downloading", "failed"].includes(job.status)) && (
                <button className="download-delete" disabled={!!pending[job.id] || deleting} onClick={() => void control(job, "delete")} aria-label={`Delete post ${job.post_id} favourite`} title="Delete favourite">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6M14 10v6" />
                  </svg>
                </button>
              )}
            </div>
          </article>
        );
      })}
    </div>
  );
}
