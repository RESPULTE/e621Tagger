import {
  useEffect,
  useRef,
  useState,
  type PointerEvent,
  type KeyboardEvent,
} from "react";

/** Arms on hold, fires only on release inside the original element. */
export function useLongPress(action: () => void, identity: number) {
  const [armed, setArmed] = useState(false);
  const gesture = useRef<{
    pointer: number;
    x: number;
    y: number;
    started: number;
    element: HTMLElement;
  } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const consumed = useRef(false);
  function cancel() {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    gesture.current = null;
    setArmed(false);
  }
  useEffect(() => {
    const outside = (e: globalThis.PointerEvent) => {
      if (gesture.current && e.pointerId !== gesture.current.pointer) cancel();
    };
    window.addEventListener("wheel", cancel, { capture: true, passive: true });
    window.addEventListener("scroll", cancel, { capture: true, passive: true });
    window.addEventListener("touchmove", cancel, {
      capture: true,
      passive: true,
    });
    window.addEventListener("pointerdown", outside, true);
    window.addEventListener("blur", cancel);
    return () => {
      cancel();
      window.removeEventListener("wheel", cancel, true);
      window.removeEventListener("scroll", cancel, true);
      window.removeEventListener("touchmove", cancel, true);
      window.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("blur", cancel);
    };
  }, [identity]);
  function down(e: PointerEvent<HTMLElement>) {
    cancel();
    consumed.current = false;
    if (
      !e.isPrimary ||
      e.button !== 0 ||
      (e.target as HTMLElement).closest("input,select,a")
    )
      return;
    const video = (e.target as HTMLElement).closest("video");
    if (video && e.clientY > video.getBoundingClientRect().bottom - 56) return;
    gesture.current = {
      pointer: e.pointerId,
      x: e.clientX,
      y: e.clientY,
      started: performance.now(),
      element: e.currentTarget,
    };
    timer.current = setTimeout(() => setArmed(true), 600);
  }
  function move(e: PointerEvent<HTMLElement>) {
    const g = gesture.current;
    if (g && Math.hypot(g.x - e.clientX, g.y - e.clientY) > 10) cancel();
  }
  function up(e: PointerEvent<HTMLElement>) {
    const g = gesture.current;
    const hit = document.elementFromPoint(e.clientX, e.clientY);
    const fire =
      g &&
      e.pointerId === g.pointer &&
      performance.now() - g.started >= 600 &&
      hit &&
      g.element.contains(hit);
    cancel();
    if (fire) {
      consumed.current = true;
      e.preventDefault();
      action();
      return true;
    }
    return false;
  }
  function key(e: KeyboardEvent<HTMLElement>) {
    if (
      e.shiftKey &&
      e.key.toLowerCase() === "s" &&
      !e.ctrlKey &&
      !e.metaKey &&
      !e.altKey
    ) {
      e.preventDefault();
      e.stopPropagation();
      action();
    }
  }
  return { armed, consumed, cancel, down, move, up, key };
}
