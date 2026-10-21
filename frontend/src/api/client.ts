import type { components } from "./schema";

export type Candidate = components["schemas"]["Candidate"];
export type CandidatePage = components["schemas"]["CandidatePage"];
export type Project = components["schemas"]["ProjectResult"];
export type Tag = components["schemas"]["TagResult"];
export type LabelResult = components["schemas"]["LabelResult"];
export type LabelState = NonNullable<Candidate["state"]>;

let csrfToken = "";
export function setCsrf(value: string) {
  csrfToken = value;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30_000);
  try {
    const response = await fetch("/api" + path, {
      ...init,
      signal: init.signal ?? controller.signal,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
        ...init.headers,
      },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const detail = body.detail;
      const message =
        typeof detail === "string"
          ? detail
          : (detail?.message ??
            (Array.isArray(detail)
              ? detail.map((item: { msg: string }) => item.msg).join("; ")
              : `Request failed (${response.status})`));
      if (response.status === 401 && !["/auth/login", "/auth/session"].includes(path))
        window.dispatchEvent(new Event("studio-session-expired"));
      throw new ApiError(response.status, message);
    }
    return await response.json();
  } catch (error) {
    if (controller.signal.aborted)
      throw new Error(
        "Request timed out. Retry this operation to confirm its result.",
      );
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export function mutationId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // Private HTTP origins lack randomUUID, but still support secure random bytes.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export const mediaUrl = (id: number, variant = "preview") =>
  `/api/media/${id}/${variant}`;
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
