import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent,
  type KeyboardEvent,
} from "react";

import { HOLD_DURATION_MS } from "./gestureSettings";
const MOVEMENT_THRESHOLD_PX = 10;

/** Arms on hold, fires only on release inside the original element. */
export function useLongPress(action: () => void, identity: number) {
  const [holding, setHolding] = useState(false);
  const [armed, setArmed] = useState(false);
  const gesture = useRef<{
    pointer: number;
    x: number;
    y: number;
    started: number;
    identity: number;
    element: HTMLElement;
  } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const consumed = useRef(false);

  const clear = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    gesture.current = null;
    setHolding(false);
    setArmed(false);
  }, []);

  const cancel = useCallback(() => {
    // Preserve suppression until the next press, so cancellation cannot open media.
    if (gesture.current) consumed.current = true;
    clear();
  }, [clear]);

  useEffect(() => {
    const outside = (e: globalThis.PointerEvent) => {
      if (gesture.current && e.pointerId !== gesture.current.pointer) cancel();
    };
    const releaseOutside = () => {
      // React's element handler runs first for an inside release.
      if (gesture.current) cancel();
    };
    window.addEventListener("wheel", cancel, { capture: true, passive: true });
    window.addEventListener("scroll", cancel, { capture: true, passive: true });
    window.addEventListener("touchmove", cancel, { capture: true, passive: true });
    window.addEventListener("pointerdown", outside, true);
    window.addEventListener("pointerup", releaseOutside);
    window.addEventListener("pointercancel", cancel, true);
    window.addEventListener("blur", cancel);
    return () => {
      cancel();
      window.removeEventListener("wheel", cancel, true);
      window.removeEventListener("scroll", cancel, true);
      window.removeEventListener("touchmove", cancel, true);
      window.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("pointerup", releaseOutside);
      window.removeEventListener("pointercancel", cancel, true);
      window.removeEventListener("blur", cancel);
    };
  }, [identity, cancel]);

  function down(e: PointerEvent<HTMLElement>) {
    cancel();
    if (!e.isPrimary) return;
    consumed.current = false;
    if (
      e.button !== 0 ||
      (e.target as Element).closest("input,textarea,select,a,[contenteditable]")
    ) return;
    const video = (e.target as Element).closest("video");
    if (video && e.clientY > video.getBoundingClientRect().bottom - 56) return;
    const current = {
      pointer: e.pointerId,
      x: e.clientX,
      y: e.clientY,
      started: performance.now(),
      identity,
      element: e.currentTarget,
    };
    gesture.current = current;
    setHolding(true);
    timer.current = setTimeout(() => {
      timer.current = null;
      if (gesture.current === current && current.element.isConnected)
        setArmed(true);
    }, HOLD_DURATION_MS);
  }

  function move(e: PointerEvent<HTMLElement>) {
    const g = gesture.current;
    if (
      g && e.pointerId === g.pointer &&
      Math.hypot(g.x - e.clientX, g.y - e.clientY) > MOVEMENT_THRESHOLD_PX
    ) cancel();
  }

  function up(e: PointerEvent<HTMLElement>) {
    const g = gesture.current;
    if (g && e.pointerId !== g.pointer) {
      cancel();
      return true;
    }
    const hit = g ? document.elementFromPoint(e.clientX, e.clientY) : null;
    const inside = Boolean(g && hit && g.element.contains(hit));
    const stationary = Boolean(g &&
      Math.hypot(g.x - e.clientX, g.y - e.clientY) <= MOVEMENT_THRESHOLD_PX);
    const held = Boolean(g && performance.now() - g.started >= HOLD_DURATION_MS);
    const valid = Boolean(g && inside && stationary &&
      g.element.isConnected && g.identity === identity);
    if (g && (!valid || held)) consumed.current = true;
    clear();
    if (held && valid) {
      consumed.current = true;
      e.preventDefault();
      action();
      return true;
    }
    if (consumed.current) {
      e.preventDefault();
      return true;
    }
    return false;
  }

  function key(e: KeyboardEvent<HTMLElement>) {
    if (
      !e.repeat && e.shiftKey && e.key.toLowerCase() === "s" &&
      !e.ctrlKey && !e.metaKey && !e.altKey &&
      !(e.target as Element).closest("input,textarea,select,[contenteditable]")
    ) {
      e.preventDefault();
      e.stopPropagation();
      action();
    }
  }
  return { holding, armed, consumed, cancel, down, move, up, key };
}
