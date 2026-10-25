import { TaskProgress } from "../components/TaskProgress";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText, mutationId } from "../api/client";
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
};
export function Downloads() {
  const client = useQueryClient();
  const [error, setError] = useState("");
  const query = useQuery({
    queryKey: ["downloads"],
    queryFn: () => api<{ items: Download[]; active: number }>("/downloads"),
    refetchInterval: 750,
  });
  async function control(id: number, action: string) {
    try {
      setError("");
      await api(`/downloads/${id}/${action}`, {
        method: "POST",
        body: JSON.stringify({ request_id: mutationId() }),
      });
      await client.invalidateQueries({ queryKey: ["downloads"] });
      void client.invalidateQueries({ queryKey: ["favorite"] });
    } catch (e) {
      setError(errorText(e));
    }
  }
  return (
    <div className="downloads">
      <p className="muted">
        {query.data?.active ?? 0} active · Collection originals go to Smut;
        dataset exports stay with their frozen version.
      </p>
      {(error || query.error) && (
        <p role="alert" className="error">
          {error || errorText(query.error)}
        </p>
      )}
      {!query.data?.items.length && !query.isPending && (
        <p>
          No downloads yet. Use a heart to save an original, or export reviewed
          dataset media.
        </p>
      )}
      {query.data?.items.map((job) => (
        <article className="download-row" key={job.id}>
          <div>
            <strong>{job.purpose}</strong>
            <span className="muted"> → {job.destination}</span>
            <span className="download-state" role="status">
              {job.status === "done"
                ? "Completed · verified"
                : job.status === "pending"
                  ? "Queued"
                  : job.status === "running"
                    ? "Downloading"
                    : job.status}
            </span>
          </div>
          <div className="download-progress">
            <TaskProgress
              active={["pending", "running"].includes(job.status)}
              value={job.progress_bytes}
              total={job.total_bytes ?? undefined}
              label={`${job.purpose} progress`}
            />
            <span>
              {(job.progress_bytes / 1048576).toFixed(1)} MiB
              {job.total_bytes
                ? ` / ${(job.total_bytes / 1048576).toFixed(1)} MiB`
                : ""}
            </span>
            {["pending", "running"].includes(job.status) && (
              <button onClick={() => void control(job.id, "cancel")}>
                Cancel
              </button>
            )}
            {["failed", "cancelled"].includes(job.status) && (
              <button onClick={() => void control(job.id, "retry")}>
                Retry
              </button>
            )}
            {job.status === "done" && job.dataset_id && (
              <>
                <a
                  href={`/api/datasets/${job.dataset_id}/media/files/${job.post_id}`}
                >
                  File
                </a>
                <a href={`/api/datasets/${job.dataset_id}/media/manifest`}>
                  Checked manifest
                </a>
                <a href={`/api/datasets/${job.dataset_id}/media/bundle`}>
                  Bundle
                </a>
              </>
            )}
          </div>
          {job.error && <p className="error">{job.error}</p>}
        </article>
      ))}
    </div>
  );
}
