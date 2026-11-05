import { HoldIndicator } from "./HoldIndicator";
import { StudioIcon } from "./StudioIcon";
import { useEffect, useRef, useState, type HTMLAttributes, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText, mutationId, type Candidate } from "../api/client";
import { useFindSimilar } from "./Similarity";
import { useLongPress } from "./useLongPress";
import { HOLD_DURATION_MS } from "./gestureSettings";
import type { components } from "../api/schema";
import { useMediaSamples, type MediaSample } from "./mediaSamples";

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
const sampleRequests = new Map<string, Promise<boolean>>();

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
  const lastIntent = useRef<boolean | undefined>(undefined);
  async function change(wanted?: boolean): Promise<boolean> {
    // Serialize taps across mounted views; a second toggle must not be swallowed
    // while the first HTTP acknowledgement is still pending.
    while (requests.has(post.post_id)) {
      if (!(await requests.get(post.post_id))) return false;
    }
    const run = (async () => {
      setBusy(true);
      setError("");
      try {
        const current = client.getQueryData<Saved>(key) ?? saved;
        wanted ??= !current.wanted;
        lastIntent.current = wanted;
        if (wanted) {
          const available = await client.fetchQuery({ queryKey: ["library-capability"],
            queryFn: () => api<Capability>("/library/status"), staleTime: 30000 });
          if (!available.available || !post.has_original || post.unavailable)
            throw new Error(available.message ?? "Collection storage is unavailable");
        }
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
        void client.invalidateQueries({ queryKey: ["candidates"] });
        return true;
      } catch (e) {
        setError(errorText(e));
        void query.refetch();
        return false;
      } finally {
        setBusy(false);
      }
    })();
    requests.set(post.post_id, run);
    try { return await run; }
    finally { if (requests.get(post.post_id) === run) requests.delete(post.post_id); }
  }
  return {
    saved,
    change,
    retry: () => change(lastIntent.current ?? saved.wanted),
    busy,
    error: error || saved.error || "",
    waiting: active.has(saved.status),
    enabled:
      !!capability.data?.available && post.has_original && !post.unavailable,
    message: capability.data?.message ?? "Checking collection storage…",
  };
}

/** Shared tap/hold arbitration for thumbnails and original image/video media. */
function useMediaInteractions(project: number, post: Candidate, onOpen?: () => void, sample?: MediaSample,
  onSampleChange?: (sample: MediaSample) => void, allowSimilarity = true) {
  const save = useCollectionSave(project, post);
  const samples = useMediaSamples(project, post, !!sample);
  const [samplePending, setSamplePending] = useState(0);
  const client = useQueryClient();
  const focusKey = sample ? `${project}:${post.post_id}:${post.md5}:${sample.id}` : "";
  const focusedSample = useRef(focusKey);
  focusedSample.current = focusKey;
  useEffect(() => () => { focusedSample.current = ""; }, []);
  const currentSample = sample && (samples.samples.find((item) => item.id === sample.id) ?? sample);
  async function change() {
    if (!currentSample) return save.change();
    const selected = currentSample;
    const requestKey = `${project}:${post.post_id}:${selected.id}`;
    setSamplePending((pending) => pending + 1);
    try {
      while (sampleRequests.has(requestKey)) {
        if (!(await sampleRequests.get(requestKey))) return false;
      }
      const run = (async () => {
        const cached = client.getQueryData<MediaSample[]>(["media-samples", project, post.post_id, post.md5]);
        const latest = cached?.find((item) => item.id === selected.id) ?? selected;
        const updated = await samples.update(latest, { favorite: !latest.favorite });
        if (updated && focusedSample.current === focusKey) onSampleChange?.(updated);
        return !!updated;
      })();
      sampleRequests.set(requestKey, run);
      try { return await run; }
      finally { if (sampleRequests.get(requestKey) === run) sampleRequests.delete(requestKey); }
    } finally { setSamplePending((pending) => pending - 1); }
  }
  const similar = useFindSimilar(project, post.post_id, sample?.image_url);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const last = useRef<{ at: number; x: number; y: number } | null>(null);
  const surface = useRef<HTMLElement | null>(null);
  const suppressVideoClick = useRef(false);
  const down = useRef<{ at: number; x: number; y: number; pointer: number; element: HTMLElement } | null>(null);
  function cancel() {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    last.current = null;
    down.current = null;
  }
  const hold = useLongPress(() => { cancel(); void similar.find(); }, post.post_id);
  useEffect(() => { hold.cancel(); cancel(); }, [project, post.post_id, post.md5, sample?.id, sample?.image_url, allowSimilarity]);
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (!event.isPrimary || (surface.current && !surface.current.contains(event.target as Node))) cancel();
    };
    for (const event of ["wheel", "touchmove"]) window.addEventListener(event, cancel, { capture: true, passive: true });
    window.addEventListener("pointerdown", outside, true);
    window.addEventListener("blur", cancel);
    return () => {
      cancel();
      for (const event of ["wheel", "touchmove"]) window.removeEventListener(event, cancel, true);
      window.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("blur", cancel);
    };
  }, [post.post_id, project, sample?.id]);
  const handlers: HTMLAttributes<HTMLElement> = {
    title: `Double tap to toggle favourite · F on focused media · hold ${HOLD_DURATION_MS / 1000} second for similar · Shift+S`,
    onDragStart: (event) => event.preventDefault(),
    onContextMenu: (event) => event.preventDefault(),
    onKeyDown: (event) => {
      if (sample && event.key.toLowerCase() === "z" && !event.ctrlKey && !event.metaKey && !event.altKey &&
          !(event.target as Element).closest("input,textarea,select,[contenteditable]")) event.stopPropagation();
      if (allowSimilarity) hold.key(event);
      if (!event.repeat && event.key.toLowerCase() === "f" && !event.ctrlKey && !event.metaKey && !event.altKey &&
          !(event.target as Element).closest("input,textarea,select,video,[contenteditable]")) {
        event.preventDefault(); event.stopPropagation(); cancel(); void change();
      }
    },
    onPointerDown: (event) => {
      suppressVideoClick.current = false;
      if (sample) event.stopPropagation();
      surface.current = event.currentTarget;
      if (!event.isPrimary || event.button !== 0 ||
          ((event.target as Element).closest("input,textarea,select,a,button,[contenteditable]") && (event.target as Element).closest("input,textarea,select,a,button,[contenteditable]") !== event.currentTarget)) { cancel(); return; }
      const video = (event.target as Element).closest("video");
      // Keep seeking, volume and other native video controls independent of gestures.
      if (video && event.clientY > video.getBoundingClientRect().bottom - 56) { cancel(); return; }
      if (allowSimilarity) hold.down(event);
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
      down.current = { at: performance.now(), x: event.clientX, y: event.clientY, pointer: event.pointerId, element: event.currentTarget };
    },
    onPointerMove: (event) => {
      if (sample) event.stopPropagation();
      if (allowSimilarity) hold.move(event);
      const d = down.current;
      if (d && Math.hypot(event.clientX - d.x, event.clientY - d.y) > 10) cancel();
    },
    onPointerLeave: () => {
      hold.cancel();
      // Touch emits leave after up: preserve that completed first tap.
      if (down.current) cancel();
    },
    onPointerCancel: () => { hold.cancel(); cancel(); },
    onPointerUp: (event) => {
      if (sample) event.stopPropagation();
      if (allowSimilarity && hold.up(event)) { cancel(); return; }
      const d = down.current;
      down.current = null;
      const hit = document.elementFromPoint(event.clientX, event.clientY);
      if (!d || d.pointer !== event.pointerId || !hit || !d.element.contains(hit) || performance.now() - d.at > 500) { cancel(); return; }
      const previous = last.current;
      if (previous && performance.now() - previous.at < 300 && Math.hypot(event.clientX - previous.x, event.clientY - previous.y) < 24) {
        cancel(); event.preventDefault();
        // Native video handles the second click before a bubbling dblclick handler.
        // Consume only our recognised body gesture; keep native controls independent.
        suppressVideoClick.current = !!(event.target as Element).closest("video");
        void change();
      } else {
        last.current = { at: performance.now(), x: event.clientX, y: event.clientY };
        timer.current = setTimeout(() => { cancel(); onOpen?.(); }, 300);
      }
    },
    onClickCapture: (event) => {
      if (suppressVideoClick.current && (event.target as Element).closest("video")) {
        suppressVideoClick.current = false;
        event.preventDefault();
        event.stopPropagation();
      }
    },
    onClick: (event) => {
      if (sample) event.stopPropagation();
      if (hold.consumed.current && event.detail !== 0) { event.preventDefault(); return; }
      if (event.detail === 0) { cancel(); onOpen?.(); }
    },
    onDoubleClickCapture: (event) => {
      if (sample) event.stopPropagation();
      const video = (event.target as Element).closest("video");
      if (video && event.clientY > video.getBoundingClientRect().bottom - 56) return;
      const control = (event.target as Element).closest("input,textarea,select,a,button,[contenteditable]");
      if (control && control !== event.currentTarget) return;
      event.preventDefault();
      if (video) event.stopPropagation();
    },
  };
  const status = currentSample ? (samplePending > 0 ? "Updating sample favourite..." : currentSample.favorite ? "Sample favourited." : "Sample not favourited.") : save.busy ? "Updating favourite…" : save.waiting
    ? save.saved.wanted ? "Favourite queued; original is not yet saved." : "Removing saved files and partial downloads…"
    : save.saved.status === "saved" ? "Original saved."
    : save.saved.wanted ? "Favourite selected; download not complete." : "Not favourited.";
  return { handlers, hold, overlays: <>
    <HoldIndicator holding={hold.holding} armed={hold.armed} />
    {(currentSample ? currentSample.favorite : save.saved.wanted) && <span className={`favorite-paw${!currentSample && save.waiting ? " is-pending" : ""}`} role="img" aria-label={!currentSample && save.waiting ? "Favourited; saving" : "Favourited"}><StudioIcon name="paw" /></span>}
  </>, notice: <>
    {similar.notice}
    <span className="sr-only" role="status">{status}</span>
    {currentSample ? samples.error && <span className="favorite-error" role="alert">{samples.error}</span> :
      save.error && <span className="favorite-error" role="alert">{save.error} <button onClick={() => void save.retry()}>Retry favourite change</button></span>}
  </> };
}

export function SaveThumbnail({ project, post, onOpen, children }: {
  project: number; post: Candidate; onOpen: () => void; children: ReactNode;
}) {
  const media = useMediaInteractions(project, post, onOpen);
  return <>
    <button {...media.handlers} className={`thumbnail-link ${media.hold.armed ? "similarity-armed" : ""}`} aria-label={`Open post ${post.post_id}`}>
      {children}{media.overlays}
    </button>
    {media.notice}
  </>;
}

export function SaveMedia({ project, post, actualSize = false, children, sample, onSampleChange, allowSimilarity = true }: {
  project: number; post: Candidate; actualSize?: boolean; children: ReactNode;
  sample?: MediaSample; onSampleChange?: (sample: MediaSample) => void; allowSimilarity?: boolean;
}) {
  const media = useMediaInteractions(project, post, undefined, sample, onSampleChange, allowSimilarity);
  return <>
    <div {...media.handlers} className={`media-stage ${actualSize ? "actual-size" : ""} ${media.hold.armed ? "similarity-armed" : ""}`}
      role="group" tabIndex={0} aria-label={`${sample ? "Sample" : "Post"} ${post.post_id} media${allowSimilarity ? "; hold to find similar" : ""}`} >
      {children}{media.overlays}
    </div>
    {media.notice}
  </>;
}
