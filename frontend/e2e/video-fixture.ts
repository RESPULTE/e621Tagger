import { readFile } from "node:fs/promises";
import type { Page } from "@playwright/test";

export async function decodesBundledVideo(page: Page) {
  // Probe a known local clip outside Studio: never skip on an app playback failure.
  const bytes = await readFile(
    new URL("../../backend/app/assets/demo.mp4", import.meta.url),
  );
  await page.goto("about:blank");
  const decoded = await page.evaluate(
    async (source) => {
      const player = document.createElement("video");
      player.muted = true;
      player.src = source;
      document.body.appendChild(player);
      return new Promise<boolean>((resolve) => {
        const finish = (ok: boolean) => {
          clearTimeout(timer);
          player.onloadeddata = null;
          player.onerror = null;
          player.removeAttribute("src");
          player.load();
          player.remove();
          resolve(ok);
        };
        const timer = setTimeout(() => finish(false), 5000);
        player.onloadeddata = () => {
          try {
            const canvas = document.createElement("canvas");
            canvas.width = canvas.height = 1;
            const context = canvas.getContext("2d")!;
            context.drawImage(player, 0, 0, 1, 1);
            context.getImageData(0, 0, 1, 1);
            finish(player.readyState >= 2 && player.videoWidth > 0 && player.videoHeight > 0);
          } catch { finish(false); }
        };
        player.onerror = () => finish(false);
      });
    },
    `data:video/mp4;base64,${bytes.toString("base64")}`,
  );
  if (!decoded && process.env.STUDIO_E2E_REQUIRE_VIDEO === "1")
    throw new Error("Required video validation failed: WebKit must decode and read back the H264 fixture; codec skips are disabled.");
  return decoded;
}
