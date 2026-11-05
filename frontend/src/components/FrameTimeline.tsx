import { useRef, type PointerEvent } from "react";
import type { MediaSample } from "./mediaSamples";
type Props = {
  duration: number;
  time: number;
  samples: MediaSample[];
  onSeek: (ms: number) => void;
  onCapture: () => void;
  onSelect: (sample: MediaSample) => void;
};
export function FrameTimeline({
  duration,
  time,
  samples,
  onSeek,
  onCapture,
  onSelect,
}: Props) {
  const track = useRef<HTMLDivElement>(null);
  const gesture = useRef<{ pointer: number; x: number; moved: boolean } | null>(
    null,
  );
  const tap = useRef<{ time: number; x: number } | null>(null);
  const clamp = (value: number) => Math.max(0, Math.min(duration, value));
  const seekAt = (x: number) => {
    const rect = track.current?.getBoundingClientRect();
    if (rect?.width) onSeek(clamp(((x - rect.left) / rect.width) * duration));
  };
  const down = (event: PointerEvent<HTMLElement>, cursor: boolean) => {
    if (!event.isPrimary || event.button !== 0) return;
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    gesture.current = {
      pointer: event.pointerId,
      x: event.clientX,
      moved: !cursor,
    };
    if (!cursor) seekAt(event.clientX);
  };
  const move = (event: PointerEvent<HTMLElement>) => {
    const g = gesture.current;
    if (!g || g.pointer !== event.pointerId) return;
    event.stopPropagation();
    if (Math.abs(event.clientX - g.x) > 5) g.moved = true;
    if (g.moved) {
      tap.current = null;
      seekAt(event.clientX);
    }
  };
  const up = (event: PointerEvent<HTMLElement>, cursor: boolean) => {
    const g = gesture.current;
    if (!g || g.pointer !== event.pointerId) return;
    event.stopPropagation();
    gesture.current = null;
    if (cursor && !g.moved) {
      const now = performance.now();
      if (
        tap.current &&
        now - tap.current.time < 350 &&
        Math.abs(event.clientX - tap.current.x) < 24
      ) {
        tap.current = null;
        onCapture();
      } else tap.current = { time: now, x: event.clientX };
    }
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
      onCapture();
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
        onPointerUp={(event) => up(event, false)}
        onPointerCancel={() => {
          gesture.current = null;
          tap.current = null;
        }}
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
          style={{ left: `${duration ? (time / duration) * 100 : 0}%` }}
          aria-label="Capture current frame"
          title="Double tap to capture frame"
          onPointerDown={(event) => down(event, true)}
          onPointerMove={move}
          onPointerUp={(event) => up(event, true)}
          onPointerCancel={() => {
            gesture.current = null;
            tap.current = null;
          }}
          onKeyDown={keyboard}
        />
      </div>
      <span className="frame-time">
        {(time / 1000).toFixed(2)} / {(duration / 1000).toFixed(2)}s
      </span>
    </div>
  );
}
