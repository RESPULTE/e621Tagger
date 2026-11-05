import { useEffect, useRef, useState } from "react";
import { api, errorText, mediaUrl, type Candidate } from "../api/client";
import { SaveMedia } from "./FavoriteButton";
import { FrameTimeline } from "./FrameTimeline";
import type { MediaSample } from "./mediaSamples";
import "./animated-media.css";

type Animation = {
  animated: boolean;
  duration_ms: number;
  frames: { index: number; timestamp_ms: number; duration_ms: number }[];
  width: number;
  height: number;
};
type Props = {
  project: number;
  post: Candidate;
  samples: MediaSample[];
  focused?: MediaSample;
  setFocused: (sample?: MediaSample) => void;
  onAnimationDetected: (animated: boolean) => void;
  capture: (
    blob: Blob,
    time: number | null,
  ) => Promise<MediaSample | undefined>;
};
export function AnimatedMedia({
  project,
  post,
  samples,
  focused,
  setFocused,
  onAnimationDetected,
  capture,
}: Props) {
  const video = useRef<HTMLVideoElement>(null);
  const focusIntent = useRef(0);
  const mounted = useRef(true),
    busy = useRef(false),
    selecting = useRef(false);
  useEffect(() => {
    mounted.current = true;
    const player = video.current;
    return () => {
      mounted.current = false;
      if (player) {
        player.pause();
        player.removeAttribute("src");
        player.load();
      }
    };
  }, []);
  useEffect(() => {
    if (!videoType) return;
    let id = 0;
    const tick = () => {
      const player = video.current;
      if (player && !player.paused) setTime(player.currentTime * 1000);
      id = requestAnimationFrame(tick);
    };
    id = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(id);
  }, []);
  const [enabled, setEnabled] = useState(false),
    [fallback, setFallback] = useState(false),
    [ready, setReady] = useState(false);
  const [duration, setDuration] = useState(0),
    [time, setTime] = useState(0),
    [playing, setPlaying] = useState(false),
    [animation, setAnimation] = useState<Animation>(),
    [error, setError] = useState("");
  const videoType = ["mp4", "webm"].includes(post.media_ext ?? "");
  const source = encodeURIComponent(post.md5 ?? "");
  const frames = animation?.frames ?? [];
  const frame = frames.reduce<(typeof frames)[number] | undefined>(
    (current, item) => (item.timestamp_ms <= time ? item : current),
    frames[0],
  );
  const frameUrl = frame
    ? `/api/posts/${post.post_id}/animation/frames/${frame.index}?source_md5=${source}`
    : "";
  useEffect(() => {
    if (animation) onAnimationDetected(animation.animated);
  }, [animation, onAnimationDetected]);
  useEffect(() => {
    if (!playing || !animation?.duration_ms) return;
    let last = performance.now();
    const timer = window.setInterval(() => {
      const now = performance.now();
      setTime((value) => (value + now - last) % animation.duration_ms);
      last = now;
    }, 40);
    return () => clearInterval(timer);
  }, [playing, animation]);
  useEffect(() => {
    if (!["webp", "avif"].includes(post.media_ext ?? "")) return;
    let active = true;
    void api<Animation>(`/posts/${post.post_id}/animation?source_md5=${source}`)
      .then((result) => {
        if (active) {
          setAnimation(result);
          if (result.animated) setDuration(result.duration_ms);
        }
      })
      .catch(() => {
        /* The explicit sampling control reports decoding errors. */
      });
    return () => {
      active = false;
    };
  }, [post.post_id, source, post.media_ext]);
  async function enable() {
    setError("");
    if (!videoType) {
      try {
        const result =
          animation ??
          (await api<Animation>(
            `/posts/${post.post_id}/animation?source_md5=${source}`,
          ));
        if (!result.animated || !result.frames.length)
          throw new Error("This image does not contain a supported animation.");
        setAnimation(result);
        setDuration(result.duration_ms);
      } catch (e) {
        setError(errorText(e));
        return;
      }
    }
    if (!mounted.current) return;
    setEnabled(true);
    setFocused(undefined);
  }
  function seek(ms: number) {
    focusIntent.current++;
    setFocused(undefined);
    setPlaying(false);
    if (videoType && video.current) {
      video.current.pause();
      if (Math.abs(video.current.currentTime - ms / 1000) > 0.0001) {
        selecting.current = true;
        video.current.currentTime = ms / 1000;
      }
    } else setTime(ms);
  }
  function pause() {
    video.current?.pause();
    setPlaying(false);
  }
  async function waitForFrame(player: HTMLVideoElement) {
    if (!player.seeking && player.readyState >= 2) return;
    // A double tap can arrive before an earlier timeline seek has decoded.
    // Wait for the browser instead of rejecting a valid capture gesture.
    await new Promise<void>((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timeout);
        player.removeEventListener("seeked", check);
        player.removeEventListener("loadeddata", check);
        player.removeEventListener("error", fail);
        player.removeEventListener("emptied", fail);
      };
      const check = () => {
        if (!player.seeking && player.readyState >= 2) {
          cleanup();
          resolve();
        }
      };
      const fail = () => {
        cleanup();
        reject(new Error("Could not load the frame. Try seeking again."));
      };
      const timeout = window.setTimeout(fail, 5000);
      player.addEventListener("seeked", check);
      player.addEventListener("loadeddata", check);
      player.addEventListener("error", fail);
      player.addEventListener("emptied", fail);
      check();
    });
  }
  async function captureFrame() {
    if (busy.current) return;
    const intent = ++focusIntent.current;
    busy.current = true;
    try {
      let blob: Blob;
      let at: number;
      if (videoType) {
        const player = video.current;
        if (!player) throw new Error("Video is unavailable.");
        player.pause();
        await waitForFrame(player);
        if (!mounted.current || focusIntent.current !== intent) return;
        if (
          !player.videoWidth ||
          !player.videoHeight
        )
          throw new Error(
            "Wait for the frame to finish seeking before capturing.",
          );
        at = Math.round(player.currentTime * 1000);
        const scale = Math.min(
          1,
          4096 / Math.max(player.videoWidth, player.videoHeight),
        );
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(player.videoWidth * scale));
        canvas.height = Math.max(1, Math.round(player.videoHeight * scale));
        const context = canvas.getContext("2d");
        if (!context) throw new Error("Canvas is unavailable.");
        context.drawImage(player, 0, 0, canvas.width, canvas.height);
        blob = await new Promise<Blob>((resolve, reject) =>
          canvas.toBlob(
            (value) =>
              value
                ? resolve(value)
                : reject(new Error("Could not capture this frame.")),
            "image/png",
          ),
        );
      } else {
        if (!frame) throw new Error("Frame is not available.");
        setPlaying(false);
        at = frame.timestamp_ms;
        const response = await fetch(frameUrl, { credentials: "same-origin" });
        if (!response.ok) throw new Error("Could not load this frame.");
        blob = await response.blob();
      }
      if (!mounted.current || focusIntent.current !== intent) return;
      const sample = await capture(blob, at);
      if (sample && mounted.current && focusIntent.current === intent) {
        setFocused(sample);
        setError("");
      }
    } catch (e) {
      if (mounted.current) setError(errorText(e));
    } finally {
      busy.current = false;
    }
  }
  function select(sample: MediaSample) {
    focusIntent.current++;
    video.current?.pause();
    setPlaying(false);
    if (video.current && sample.timestamp_ms != null) {
      if (
        Math.abs(video.current.currentTime - sample.timestamp_ms / 1000) >
        0.0001
      ) {
        selecting.current = true;
        video.current.currentTime = sample.timestamp_ms / 1000;
      }
    }
    setTime(sample.timestamp_ms ?? 0);
    setFocused(sample);
  }
  return (
    <div className="animated-viewer">
      <SaveMedia
        project={project}
        post={post}
        actualSize={false}
        sample={focused}
        onSampleChange={(updated) => setFocused(updated)}
        allowSimilarity={!!focused || animation?.animated === false}
      >
        {focused && (
          <img
            key={focused.id}
            className={`sample-image sample-${focused.state ?? "undecided"}${videoType ? " sample-video-overlay" : ""}`}
            src={focused.image_url}
            alt={
              focused.kind === "thumbnail"
                ? "Sampled thumbnail"
                : `Sampled frame at ${((focused.timestamp_ms ?? 0) / 1000).toFixed(2)} seconds`
            }
          />
        )}
        {!enabled && !videoType && !focused && (
          <img
            src={mediaUrl(post.post_id, "original", post.md5)}
            alt={`Original post ${post.post_id}`}
          />
        )}
        {videoType && (
          <video
            ref={video}
            controls
            loop
            playsInline
            preload="metadata"
            src={mediaUrl(
              post.post_id,
              fallback ? "video_mp4" : "original",
              post.md5,
            )}
            onLoadedMetadata={(event) => {
              setDuration(event.currentTarget.duration * 1000);
              setReady(true);
            }}
            onTimeUpdate={(event) =>
              setTime(event.currentTarget.currentTime * 1000)
            }
            onSeeking={() => {
              if (!selecting.current) {
                focusIntent.current++;
                setFocused(undefined);
              }
              selecting.current = false;
            }}
            onSeeked={(event) => {
              const player = event.currentTarget;
              setTime(player.currentTime * 1000);
            }}
            onPlay={() => {
              focusIntent.current++;
              setFocused(undefined);
            }}
            onError={() => {
              if (!fallback && post.has_video_fallback) setFallback(true);
              else setError("Video could not be loaded.");
            }}
          />
        )}
        {enabled && !videoType && !focused && (
          <img
            src={frameUrl}
            alt={`Animation frame at ${(time / 1000).toFixed(2)} seconds`}
          />
        )}
      </SaveMedia>
      <div className="animation-controls">
        {!enabled && animation?.animated !== false ? (
          <button onClick={() => void enable()}>Enable frame sampling</button>
        ) : enabled ? (
          <>
            {!videoType && (
              <button
                onClick={() => {
                  setFocused(undefined);
                  setPlaying((value) => !value);
                }}
              >
                {playing ? "Pause" : "Play"}
              </button>
            )}
          </>
        ) : null}
      </div>
      {enabled && duration > 0 && (
        <FrameTimeline
          duration={duration}
          time={time}
          samples={samples}
          onSeek={seek}
          onPause={pause}
          onCapture={() => void captureFrame()}
          onSelect={select}
        />
      )}
      {enabled && videoType && !ready && !error && (
        <p role="status">Loading video metadata…</p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}{" "}
          <a href={`https://e621.net/posts/${post.post_id}`} target="_blank" rel="noreferrer">Open source post</a>
        </p>
      )}
    </div>
  );
}
