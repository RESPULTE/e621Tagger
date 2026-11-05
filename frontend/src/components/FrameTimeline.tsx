import { useCallback, useEffect, useRef, useState, type PointerEvent } from "react";
import { HOLD_DURATION_MS } from "./gestureSettings";
import { HoldIndicator } from "./HoldIndicator";
import type { MediaSample } from "./mediaSamples";
type Props = {
  duration: number;
  time: number;
  samples: MediaSample[];
  onSeek: (ms: number) => void;
  onPause: () => void;
  onCapture: () => void;
  onSelect: (sample: MediaSample) => void;
};
export function FrameTimeline({
  duration,
  time,
  samples,
  onSeek,
  onPause,
  onCapture,
  onSelect,
}: Props) {
  const track = useRef<HTMLDivElement>(null);
  const gesture = useRef<{
    pointer: number; x: number; y: number; mode: "pending" | "scrub" | "pull" | "done";
  } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const capture = useRef(onCapture);
  capture.current = onCapture;
  const [holding, setHolding] = useState(false);
  const [pull, setPull] = useState(0);
  const stopHold = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    setHolding(false);
    setPull(0);
  }, []);
  const cancel = useCallback(() => {
    gesture.current = null;
    stopHold();
  }, [stopHold]);
  useEffect(() => {
    const outsidePointer = (event: globalThis.PointerEvent) => {
      if (gesture.current && event.pointerId !== gesture.current.pointer) cancel();
    };
    window.addEventListener("blur", cancel);
    window.addEventListener("scroll", cancel, true);
    window.addEventListener("pointerdown", outsidePointer, true);
    return () => {
      cancel();
      window.removeEventListener("blur", cancel);
      window.removeEventListener("scroll", cancel, true);
      window.removeEventListener("pointerdown", outsidePointer, true);
    };
  }, [cancel]);
  const clamp = (value: number) => Math.max(0, Math.min(duration, value));
  const seekAt = (x: number) => {
    const rect = track.current?.getBoundingClientRect();
    if (rect?.width) onSeek(clamp(((x - rect.left) / rect.width) * duration));
  };
  const down = (event: PointerEvent<HTMLElement>, cursor: boolean) => {
    if (!event.isPrimary || event.button !== 0) return;
    event.stopPropagation();
    cancel();
    if (cursor) onPause();
    event.currentTarget.setPointerCapture(event.pointerId);
    gesture.current = {
      pointer: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      mode: cursor ? "pending" : "scrub",
    };
    if (!cursor) {
      seekAt(event.clientX);
    }
  };
  const move = (event: PointerEvent<HTMLElement>) => {
    const g = gesture.current;
    if (!g || g.pointer !== event.pointerId) return;
    event.stopPropagation();
    if (g.mode === "done") return;
    const dx = Math.abs(event.clientX - g.x);
    const dy = event.clientY - g.y;
    if (g.mode !== "scrub") {
      if (dx > 12) {
        stopHold();
        g.mode = "scrub";
      } else if (dy >= 18) {
        setPull(Math.min(dy, 36));
        if (g.mode !== "pull") {
          g.mode = "pull";
          setHolding(true);
          timer.current = setTimeout(() => {
            if (gesture.current !== g || g.mode !== "pull") return;
            g.mode = "done";
            stopHold();
            capture.current();
          }, HOLD_DURATION_MS);
        }
      } else {
        stopHold();
        g.mode = "pending";
      }
    }
    if (g.mode === "scrub") {
      seekAt(event.clientX);
    }
  };
  const up = (event: PointerEvent<HTMLElement>) => {
    const g = gesture.current;
    if (!g || g.pointer !== event.pointerId) return;
    event.stopPropagation();
    cancel();
  };
  const keyboard = (event: React.KeyboardEvent) => {
    let next: number | undefined;
    if (event.key === "ArrowLeft" || event.key === "ArrowDown")
      next = time - 100;
    if (event.key === "ArrowRight" || event.key === "ArrowUp")
      next = time + 100;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = duration;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat) onCapture();
    } else if (next !== undefined) {
      event.preventDefault();
      event.stopPropagation();
      onSeek(clamp(next));
    }
  };
  return (
    <div className="frame-timeline-wrap">
      <div
        ref={track}
        className="frame-timeline"
        role="slider"
        tabIndex={0}
        aria-label="Animation position"
        aria-valuemin={0}
        aria-valuemax={Math.round(duration)}
        aria-valuenow={Math.round(time)}
        aria-valuetext={`${(time / 1000).toFixed(2)} seconds`}
        onPointerDown={(event) => down(event, false)}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={cancel}
        onLostPointerCapture={cancel}
        onKeyDown={keyboard}
      >
        <div className="frame-track" />
        {samples
          .filter(
            (sample) => sample.kind === "frame" && sample.timestamp_ms != null,
          )
          .map((sample) => (
            <button
              type="button"
              key={sample.id}
              className={`frame-checkpoint ${sample.state ?? "undecided"}`}
              style={{
                left: `${duration ? (sample.timestamp_ms! / duration) * 100 : 0}%`,
              }}
              aria-label={`Frame at ${(sample.timestamp_ms! / 1000).toFixed(2)} seconds, ${sample.state ?? "undecided"}`}
              onPointerDown={(event) => event.stopPropagation()}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ")
                  event.stopPropagation();
              }}
              onClick={(event) => {
                event.stopPropagation();
                onSelect(sample);
              }}
            />
          ))}
        <button
          type="button"
          className="frame-cursor"
          style={{ left: `${duration ? (time / duration) * 100 : 0}%`, transform: `translate(-50%, ${pull}px)` }}
          aria-label="Capture current frame"
          title={`Pull down and hold ${HOLD_DURATION_MS / 1000} second to capture frame · Enter to capture`}
          onPointerDown={(event) => down(event, true)}
          onPointerMove={move}
          onPointerUp={up}
          onPointerCancel={cancel}
          onLostPointerCapture={cancel}
          onKeyDown={keyboard}
        >
          <HoldIndicator holding={holding} armed={false} showIcon={false} />
        </button>
      </div>
      <span className="frame-time">
        {(time / 1000).toFixed(2)} / {(duration / 1000).toFixed(2)}s
      </span>
    </div>
  );
}
