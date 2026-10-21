import { api } from "./client";

type MetricName =
  | "gallery_first_paint_ms"
  | "gallery_renders"
  | "frame_interval_ms"
  | "label_ack_ms";
declare global {
  interface Window {
    studioMetrics?: Partial<Record<MetricName, number[]>>;
  }
}

export function recordMetric(name: MetricName, value: number) {
  const metrics = (window.studioMetrics ??= {});
  const samples = (metrics[name] ??= []);
  samples.push(value);
  if (samples.length > 128) samples.shift();
  void api("/metrics/client", {
    method: "POST",
    body: JSON.stringify({ name, value }),
  }).catch(() => {});
}

export function sampleFrames() {
  let count = 0,
    start = performance.now(),
    previous = start,
    elapsed = 0;
  function frame(now: number) {
    elapsed += now - previous;
    previous = now;
    if (++count < 60 && now - start < 3000) requestAnimationFrame(frame);
    else recordMetric("frame_interval_ms", elapsed / count);
  }
  requestAnimationFrame(frame);
}
