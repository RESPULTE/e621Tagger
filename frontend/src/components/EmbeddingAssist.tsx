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
          <Link
            to={`/projects/${project}?q=${encodeURIComponent(imported.query)}`}
          >
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
