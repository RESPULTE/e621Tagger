import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText, mutationId, type Candidate } from "../api/client";

export interface MediaSample {
  id: string;
  project_id: number;
  post_id: number;
  source_md5: string;
  kind: "thumbnail" | "frame";
  timestamp_ms: number | null;
  image_url: string;
  state: "positive" | "negative" | null;
  favorite: boolean;
  version: number;
}

const mutations = new Map<string, Promise<MediaSample | undefined>>();

export function useMediaSamples(
  project: number,
  post: Candidate | undefined,
  enabled = true,
) {
  const client = useQueryClient();
  const [mutationError, setMutationError] = useState("");
  useEffect(() => setMutationError(""), [project, post?.post_id, post?.md5]);
  const prefix = ["media-samples", project, post?.post_id];
  const key = [...prefix, post?.md5];
  const path = `/projects/${project}/posts/${post?.post_id}/samples`;
  const query = useQuery({
    queryKey: key,
    enabled: enabled && !!post,
    queryFn: ({ signal }) => api<MediaSample[]>(path, { signal }),
  });
  async function invalidate() {
    await Promise.all([
      client.invalidateQueries({ queryKey: prefix }),
      client.invalidateQueries({ queryKey: ["candidates", project] }),
      client.invalidateQueries({ queryKey: ["feed", project] }),
      client.invalidateQueries({ queryKey: ["post", project, post?.post_id] }),
      client.invalidateQueries({ queryKey: ["project", project] }),
      client.invalidateQueries({ queryKey: ["projects"] }),
    ]);
  }
  async function capture(
    blob: Blob,
    timeMs: number | null,
  ): Promise<MediaSample | undefined> {
    setMutationError("");
    try {
      if (!post?.md5)
        throw new Error(
          "The source image has no MD5; a sample cannot be saved.",
        );
      const params = new URLSearchParams({
        kind: timeMs === null ? "thumbnail" : "frame",
        source_md5: post.md5,
        request_id: mutationId(),
      });
      if (timeMs !== null) params.set("timestamp_ms", String(timeMs));
      const result = await api<MediaSample>(`${path}?${params}`, {
        method: "POST",
        body: blob,
        headers: { "Content-Type": "application/octet-stream" },
      });
      await invalidate();
      return result;
    } catch (error) {
      setMutationError(errorText(error));
      return undefined;
    }
  }
  async function update(
    sample: MediaSample,
    changes: { state?: "positive" | "negative" | null; favorite?: boolean },
  ): Promise<MediaSample | undefined> {
    const identity = `${project}:${sample.post_id}:${sample.id}`;
    while (mutations.has(identity)) {
      if (!(await mutations.get(identity))) return undefined;
    }
    const operation = (async () => {
      setMutationError("");
      try {
        if (
          !post ||
          sample.post_id !== post.post_id ||
          sample.source_md5 !== post.md5
        )
          throw new Error("The source post changed; reopen the sample.");
        const latest =
          client
            .getQueryData<MediaSample[]>(key)
            ?.find((item) => item.id === sample.id) ?? sample;
        const result = await api<MediaSample>(
          `${path}/${encodeURIComponent(sample.id)}`,
          {
            method: "PATCH",
            body: JSON.stringify({
              ...changes,
              expected_version: latest.version,
              request_id: mutationId(),
            }),
          },
        );
        client.setQueryData<MediaSample[]>(key, (old) =>
          old?.some((item) => item.id === result.id)
            ? old.map((item) => (item.id === result.id ? result : item))
            : [...(old ?? []), result],
        );
        await invalidate();
        return result;
      } catch (error) {
        setMutationError(errorText(error));
        void query.refetch();
        return undefined;
      }
    })();
    mutations.set(identity, operation);
    try {
      return await operation;
    } finally {
      if (mutations.get(identity) === operation) mutations.delete(identity);
    }
  }
  return {
    samples: query.data ?? [],
    loading: query.isLoading,
    error: mutationError || (query.error ? errorText(query.error) : ""),
    capture,
    update,
    refresh: query.refetch,
  };
}
