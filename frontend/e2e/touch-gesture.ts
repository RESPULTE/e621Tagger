import type { Page } from "@playwright/test";

type Point = { x: number; y: number };
type WebKitSession = { send: (method: string, params: object) => Promise<unknown> };

/** Native engine input, including pointer capture and touch-action arbitration. */
export async function touchGesture(page: Page) {
  const engine = page.context().browser()!.browserType().name();
  const cdp = engine === "chromium" ? await page.context().newCDPSession(page) : null;
  // Playwright exposes only taps publicly for WebKit. Its in-process page proxy
  // supports native touch sequences (verified against the locked 1.64.1 protocol).
  // Fail explicitly if an upgrade changes this seam; never fall back to a mouse.
  const webkit = engine === "webkit" ? (page as Page & {
    _connection: { toImpl: (page: Page) => { delegate: { _pageProxySession: WebKitSession } } };
  })._connection.toImpl(page).delegate._pageProxySession : null;
  if (!cdp && !webkit) throw new Error(`Native touch gestures are unsupported on ${engine}`);
  let last: Point = { x: 0, y: 0 };
  const send = async (type: "touchStart" | "touchMove" | "touchEnd" | "touchCancel", point?: Point) => {
    if (point) last = { x: Math.round(point.x), y: Math.round(point.y) };
    const touchPoints = cdp && (type === "touchEnd" || type === "touchCancel")
      ? [] : [{ ...last, id: 1 }];
    await (cdp ?? webkit)!.send("Input.dispatchTouchEvent", { type, touchPoints });
  };
  return {
    start: (point: Point) => send("touchStart", point),
    move: (point: Point) => send("touchMove", point),
    end: () => send("touchEnd"),
    cancel: () => send("touchCancel"),
    dispose: async () => { await cdp?.detach(); },
  };
}
