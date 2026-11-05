import { readFile } from "node:fs/promises";
import type { Page } from "@playwright/test";

export async function decodesBundledVideo(page: Page) {
  // Probe a known local clip outside Studio: never skip on an app playback failure.
  const bytes = await readFile(
    new URL("../../backend/app/assets/demo.mp4", import.meta.url),
  );
  await page.goto("about:blank");
  return page.evaluate(
    async (source) => {
      const player = document.createElement("video");
      player.muted = true;
      player.src = source;
      document.body.appendChild(player);
      return new Promise<boolean>((resolve) => {
        const finish = (ok: boolean) => {
          clearTimeout(timer);
          player.onloadedmetadata = null;
          player.onerror = null;
          player.removeAttribute("src");
          player.load();
          player.remove();
          resolve(ok);
        };
        const timer = setTimeout(() => finish(false), 5000);
        player.onloadedmetadata = () => finish(true);
        player.onerror = () => finish(false);
      });
    },
    `data:video/mp4;base64,${bytes.toString("base64")}`,
  );
}
