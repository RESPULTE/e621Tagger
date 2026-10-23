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
