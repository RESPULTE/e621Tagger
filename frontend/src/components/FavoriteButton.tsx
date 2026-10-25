import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText, mutationId, type Candidate } from "../api/client";
import { useFindSimilar } from "./Similarity";
import { useLongPress } from "./useLongPress";
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
const requests = new Map<number, Promise<boolean>>();

export function useCollectionSave(project: number, post: Candidate) {
  const client = useQueryClient();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const capability = useQuery({
    queryKey: ["library-capability"],
    queryFn: () => api<Capability>("/library/status"),
    staleTime: 30000,
  });
  const key = ["favorite", post.post_id];
  const query = useQuery({
    queryKey: key,
    queryFn: () => api<Saved>(`/library/posts/${post.post_id}`),
    initialData: post.favorite,
    staleTime: Infinity,
    refetchInterval: (q) =>
      active.has(q.state.data?.status ?? "") ? 750 : false,
  });
  useEffect(() => {
    client.setQueryData<Saved>(key, (old) =>
      !old || post.favorite.updated_at > old.updated_at ? post.favorite : old,
    );
  }, [client, post.favorite, post.post_id]);
  const saved = query.data ?? post.favorite;
  async function change(wanted = true): Promise<boolean> {
    const pending = requests.get(post.post_id);
    if (pending) return pending;
    const run = (async () => {
      setBusy(true);
      setError("");
      try {
        const current = client.getQueryData<Saved>(key) ?? saved;
        const result = await api<Saved>(
          `/projects/${project}/library/${post.post_id}`,
          {
            method: "PUT",
            body: JSON.stringify({
              request_id: mutationId(),
              wanted,
              ...(wanted && post.md5 ? { source_md5: post.md5 } : {}),
              expected_version: current.version,
            }),
          },
        );
        client.setQueryData(key, result);
        void client.invalidateQueries({ queryKey: ["downloads"] });
        return true;
      } catch (e) {
        setError(errorText(e));
        void query.refetch();
        return false;
      } finally {
        setBusy(false);
        requests.delete(post.post_id);
      }
    })();
    requests.set(post.post_id, run);
    return run;
  }
  return {
    saved,
    change,
    busy,
    error: error || saved.error || "",
    waiting: active.has(saved.status),
    enabled:
      !!capability.data?.available && post.has_original && !post.unavailable,
    message: capability.data?.message ?? "Checking collection storage…",
  };
}

export function FavoriteButton({
  project,
  post,
  compact = false,
  allowRemoval = false,
}: {
  project: number;
  post: Candidate;
  compact?: boolean;
  allowRemoval?: boolean;
}) {
  const save = useCollectionSave(project, post);
  const text =
    save.saved.status === "saved"
      ? "Saved to Smut"
      : save.waiting
        ? "Queued / saving…"
        : save.saved.status === "cancelled"
          ? "Save cancelled"
          : "Save to Smut";
  return (
    <div className={`favorite-control ${compact ? "compact" : ""}`}>
      <button
        className={`favorite-button ${save.saved.wanted ? "hearted" : ""}`}
        aria-label={`Save post ${post.post_id} to Smut folder`}
        aria-pressed={save.saved.present}
        title={save.error || `${text}. ${save.message}`}
        disabled={save.busy || !save.enabled}
        onClick={(e) => {
          e.stopPropagation();
          void save.change();
        }}
      >
        <span aria-hidden="true">{save.saved.wanted ? "♥" : "♡"}</span>
        {compact ? (
          save.waiting ? (
            <span aria-label="Save operation pending">…</span>
          ) : null
        ) : (
          <span>{text}</span>
        )}
      </button>
      {save.error && (
        <span className="favorite-error" role="alert" title={save.error}>
          {compact ? "!" : save.error}
        </span>
      )}
      {!compact && (
        <span role="status">
          {save.saved.status === "saved"
            ? "Original and e621 tags saved."
            : save.waiting
              ? "Download queued; original is not yet saved."
              : ""}
        </span>
      )}
      {allowRemoval && save.saved.present && (
        <button
          className="quiet"
          disabled={save.busy || save.waiting}
          onClick={() => void save.change(false)}
        >
          Remove collection files
        </button>
      )}
    </div>
  );
}

export function SaveThumbnail({
  project,
  post,
  onOpen,
  children,
}: {
  project: number;
  post: Candidate;
  onOpen: () => void;
  children: React.ReactNode;
}) {
  const save = useCollectionSave(project, post);
  const similar = useFindSimilar(project, post.post_id);
  const hold = useLongPress(() => {
    cancel();
    down.current = null;
    void similar.find();
  }, post.post_id);
  const [heart, setHeart] = useState(false);
  const [notice, setNotice] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const element = useRef<HTMLButtonElement>(null);
  const pulse = useRef<ReturnType<typeof setTimeout> | null>(null);
  const last = useRef<{ at: number; x: number; y: number } | null>(null);
  const down = useRef<{
    at: number;
    x: number;
    y: number;
    moved: boolean;
  } | null>(null);
  function cancel() {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    last.current = null;
  }
  useEffect(() => {
    const stop = () => {
      cancel();
      down.current = null;
    };
    const outside = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !element.current?.contains(event.target)
      )
        stop();
    };
    // Pointer movement/cancellation and wheel/touch scrolling cancel gestures.
    // A focus-induced scroll after a stationary tap must still allow opening.
    window.addEventListener("wheel", stop, { capture: true, passive: true });
    window.addEventListener("touchmove", stop, {
      capture: true,
      passive: true,
    });
    window.addEventListener("pointerdown", outside, true);
    return () => {
      stop();
      if (pulse.current) clearTimeout(pulse.current);
      window.removeEventListener("wheel", stop, true);
      window.removeEventListener("touchmove", stop, true);
      window.removeEventListener("pointerdown", outside, true);
    };
  }, []);
  return (
    <>
      <button
        ref={element}
        className={`thumbnail-link ${hold.armed ? "similarity-armed" : ""}`}
        title="Hold then release to find similar; Shift+S"
        onKeyDown={hold.key}
        onContextMenu={(e) => e.preventDefault()}
        onPointerLeave={() => {
          hold.cancel();
          // Touch generates pointerleave after pointerup. Keep that completed
          // tap available for the second tap; cancel only an unfinished press.
          if (down.current) {
            cancel();
            down.current = null;
          }
        }}
        aria-label={`Open post ${post.post_id}`}
        onPointerDown={(e) => {
          hold.down(e);
          if (!e.isPrimary || e.button !== 0) return;
          if (timer.current) clearTimeout(timer.current);
          timer.current = null;
          down.current = {
            at: performance.now(),
            x: e.clientX,
            y: e.clientY,
            moved: false,
          };
        }}
        onPointerMove={(e) => {
          hold.move(e);
          const d = down.current;
          if (d && Math.hypot(e.clientX - d.x, e.clientY - d.y) > 10) {
            d.moved = true;
            cancel();
          }
        }}
        onPointerCancel={() => {
          hold.cancel();
          cancel();
          down.current = null;
        }}
        onPointerUp={(e) => {
          if (hold.up(e)) return;
          const d = down.current;
          down.current = null;
          if (!d || d.moved || performance.now() - d.at > 500) {
            cancel();
            return;
          }
          const previous = last.current;
          if (
            previous &&
            performance.now() - previous.at < 300 &&
            Math.hypot(e.clientX - previous.x, e.clientY - previous.y) < 24
          ) {
            cancel();
            if (!save.enabled) {
              setNotice(save.message);
              return;
            }
            void save.change().then((ok) => {
              setNotice(
                ok
                  ? save.saved.status === "saved"
                    ? "Already saved to Smut."
                    : "Save queued; check Downloads for completion."
                  : "Save request failed.",
              );
              if (ok) {
                setHeart(true);
                if (pulse.current) clearTimeout(pulse.current);
                pulse.current = setTimeout(() => setHeart(false), 700);
              }
            });
          } else {
            cancel();
            last.current = {
              at: performance.now(),
              x: e.clientX,
              y: e.clientY,
            };
            timer.current = setTimeout(() => {
              cancel();
              onOpen();
            }, 300);
          }
        }}
        onClick={(e) => {
          if (hold.consumed.current && e.detail !== 0) {
            hold.consumed.current = false;
            e.preventDefault();
            return;
          }
          if (e.detail === 0) {
            cancel();
            onOpen();
          }
        }}
        onDoubleClick={(e) => e.preventDefault()}
      >
        {children}
        {hold.armed && (
          <span className="similarity-hint">Release to find similar</span>
        )}
        {heart && (
          <span className="save-heart" aria-hidden="true">
            ♥
          </span>
        )}
      </button>
      {similar.notice}
      <span className="sr-only" role="status">
        {notice || save.error}
      </span>
    </>
  );
}
