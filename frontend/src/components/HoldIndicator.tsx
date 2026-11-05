import type { CSSProperties } from "react";
import { LONG_PRESS_DURATION_MS } from "./useLongPress";

/** Animate .hold-indicator-progress while .is-holding; .is-armed marks release readiness. */
export function HoldIndicator({ holding, armed }: { holding: boolean; armed: boolean }) {
  if (!holding && !armed) return null;
  return (
    <span
      className={`hold-indicator${holding ? " is-holding" : ""}${armed ? " is-armed" : ""}`}
      style={{ "--hold-duration": `${LONG_PRESS_DURATION_MS}ms` } as CSSProperties}
      aria-hidden="true"
    >
      <svg viewBox="0 0 48 48" fill="none" focusable="false">
        <circle className="hold-indicator-track" cx="24" cy="24" r="18" />
        <circle className="hold-indicator-progress" cx="24" cy="24" r="18" pathLength="1" />
        <g className="hold-indicator-icon" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="22" cy="24" r="6" />
          <path d="m26.5 28.5 5 5M31 14v7m-3.5-3.5h7" />
        </g>
      </svg>
    </span>
  );
}
