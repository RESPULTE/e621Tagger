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
      if (response.status === 401 && path !== "/auth/login")
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

export const mutationId = () => crypto.randomUUID();
export const mediaUrl = (id: number, variant = "preview") =>
  `/api/media/${id}/${variant}`;
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
