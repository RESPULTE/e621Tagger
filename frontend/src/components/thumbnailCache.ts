// Encoded bytes only, in RAM. React Query holds blobs only while cards are mounted.
const budget = 32 * 1024 * 1024;
const ttl = 5 * 60_000;
const entries = new Map<string, { blob: Blob; expires: number }>();
let bytes = 0;
let generation = 0;

export function clearThumbnailCache() {
  entries.clear();
  bytes = 0;
  generation++;
}
window.addEventListener("studio-session-expired", clearThumbnailCache);
window.addEventListener("studio-signed-out", clearThumbnailCache);

export async function thumbnailBlob(url: string, signal: AbortSignal) {
  for (const [key, entry] of entries) {
    if (entry.expires <= Date.now()) {
      entries.delete(key);
      bytes -= entry.blob.size;
    }
  }
  const cached = entries.get(url);
  if (cached) {
    entries.delete(url);
    entries.set(url, cached);
    return cached.blob;
  }
  const startedGeneration = generation;
  const response = await fetch(url, { signal, cache: "no-store" });
  if (response.status === 202) return null;
  if (response.status === 401)
    window.dispatchEvent(new Event("studio-session-expired"));
  if (!response.ok) throw new Error("Thumbnail unavailable");
  const blob = await response.blob();
  if (!signal.aborted && startedGeneration === generation && blob.size <= budget) {
    const old = entries.get(url);
    if (old) bytes -= old.blob.size;
    entries.delete(url);
    entries.set(url, { blob, expires: Date.now() + ttl });
    bytes += blob.size;
    while (bytes > budget) {
      const [key, entry] = entries.entries().next().value!;
      entries.delete(key);
      bytes -= entry.blob.size;
    }
  }
  return blob;
}
