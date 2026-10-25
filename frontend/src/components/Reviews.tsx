import {
  createContext,
  useContext,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import type { SimilarSearch } from "./Similarity";
import { recordMetric } from "../api/telemetry";
import {
  api,
  errorText,
  mutationId,
  type Candidate,
  type CandidatePage,
  type LabelResult,
  type LabelState,
} from "../api/client";

type Operation = {
  project: number;
  posts: number[];
  run: () => Promise<LabelResult[]>;
  optimistic: Record<number, LabelState | null>;
  undoOf?: number;
};
type UndoTarget = Pick<Candidate, "post_id" | "state" | "version" | "event_id">;
type HistoryEntry = { project: number; result: LabelResult };
type Pending = {
  state: LabelState | null;
  saving: boolean;
  error?: string;
  retry?: () => void;
};
type ReviewContext = {
  pending: Record<string, Pending>;
  edit: (
    project: number,
    posts: Candidate[],
    state: LabelState | null,
  ) => Promise<boolean>;
  undo: (project: number, post: UndoTarget) => Promise<boolean>;
  undoLast: (project: number, fallback: UndoTarget) => Promise<number | null>;
};
const Context = createContext<ReviewContext | null>(null);
export const useReviews = () => useContext(Context)!;
const keyFor = (project: number, post: number) => `${project}:${post}`;

export function ReviewProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const active = useRef(new Set<string>());
  const history = useRef<HistoryEntry[]>(readHistory());
  const [pending, setPending] = useState<Record<string, Pending>>({});

  async function execute(operation: Operation): Promise<boolean> {
    const keys = operation.posts.map((id) => keyFor(operation.project, id));
    if (keys.some((key) => active.current.has(key))) return false;
    keys.forEach((key) => active.current.add(key));
    setPending((old) => ({
      ...old,
      ...Object.fromEntries(
        operation.posts.map((id) => [
          keyFor(operation.project, id),
          { state: operation.optimistic[id], saving: true },
        ]),
      ),
    }));
    try {
      const started = performance.now();
      const results = await operation.run();
      recordMetric("label_ack_ms", performance.now() - started);
      history.current = operation.undoOf
        ? history.current.filter(
            (entry) => entry.result.event_id !== operation.undoOf,
          )
        : [
            ...history.current,
            ...results.map((result) => ({
              project: operation.project,
              result,
            })),
          ].slice(-50);
      // A disabled/full browser storage area must not turn an acknowledged save into a failure.
      try {
        sessionStorage.setItem(
          "studio-undo-history",
          JSON.stringify(history.current),
        );
      } catch {
        /* In-memory undo remains available. */
      }
      results.forEach((result) => {
        client.setQueriesData<SimilarSearch>(
          { queryKey: ["similarity", operation.project] },
          (old) =>
            old
              ? {
                  ...old,
                  results: old.results.map((entry) =>
                    entry.post_id === result.post_id && entry.candidate
                      ? {
                          ...entry,
                          candidate: { ...entry.candidate, ...result },
                        }
                      : entry,
                  ),
                }
              : old,
        );
        client.setQueryData<Candidate>(
          ["post", operation.project, result.post_id],
          (old) => (old ? { ...old, ...result } : undefined),
        );
        for (const scope of ["candidates", "feed"])
          client.setQueriesData<InfiniteData<CandidatePage>>(
            { queryKey: [scope, operation.project] },
            (old) =>
              old
                ? {
                    ...old,
                    pages: old.pages.map((page) => ({
                      ...page,
                      items: page.items.map((item) =>
                        item.post_id === result.post_id
                          ? { ...item, ...result }
                          : item,
                      ),
                    })),
                  }
                : undefined,
          );
      });
      await Promise.all([
        client.invalidateQueries({ queryKey: ["project", operation.project] }),
        client.invalidateQueries({
          queryKey: ["candidates", operation.project],
        }),
      ]);
      setPending((old) =>
        Object.fromEntries(
          Object.entries(old).filter(([key]) => !keys.includes(key)),
        ),
      );
      return true;
    } catch (error) {
      setPending((old) => ({
        ...old,
        ...Object.fromEntries(
          operation.posts.map((id) => [
            keyFor(operation.project, id),
            {
              state: operation.optimistic[id],
              saving: false,
              error: errorText(error),
              retry: () => {
                void execute(operation);
              },
            },
          ]),
        ),
      }));
      void client.invalidateQueries({ queryKey: ["post", operation.project] });
      void client.invalidateQueries({
        queryKey: ["candidates", operation.project],
      });
      return false;
    } finally {
      keys.forEach((key) => active.current.delete(key));
    }
  }

  async function edit(
    project: number,
    posts: Candidate[],
    state: LabelState | null,
  ) {
    const request_id = mutationId();
    const items = posts.map((post) => ({
      post_id: post.post_id,
      ...(post.similarity ? { similarity: post.similarity } : {}),
      expected_version: post.version,
      state,
      ...(state && post.md5 ? { source_md5: post.md5 } : {}),
    }));
    return execute({
      project,
      posts: posts.map((p) => p.post_id),
      optimistic: Object.fromEntries(posts.map((p) => [p.post_id, state])),
      run: () =>
        posts.length === 1
          ? api<LabelResult>(
              `/projects/${project}/labels/${posts[0].post_id}`,
              {
                method: "PATCH",
                body: JSON.stringify({ request_id, ...items[0] }),
              },
            ).then((value) => [value])
          : api<LabelResult[]>(`/projects/${project}/labels/batch`, {
              method: "POST",
              body: JSON.stringify({ request_id, items }),
            }),
    });
  }

  async function undo(project: number, post: UndoTarget) {
    if (!post.event_id) return false;
    const request_id = mutationId();
    return execute({
      project,
      posts: [post.post_id],
      optimistic: { [post.post_id]: post.state },
      undoOf: post.event_id,
      run: () =>
        api<LabelResult>(`/projects/${project}/labels/${post.post_id}/undo`, {
          method: "POST",
          body: JSON.stringify({
            request_id,
            event_id: post.event_id,
            expected_version: post.version,
          }),
        }).then((value) => [value]),
    });
  }

  async function undoLast(project: number, fallback: UndoTarget) {
    const latest =
      [...history.current].reverse().find((entry) => entry.project === project)
        ?.result ?? fallback;
    return (await undo(project, latest)) ? latest.post_id : null;
  }

  const failures = Object.entries(pending).filter(([, value]) => value.error);
  return (
    <Context.Provider value={{ pending, edit, undo, undoLast }}>
      {children}
      {failures.length > 0 && (
        <aside className="save-errors" role="alert">
          <strong>{failures.length} save(s) need attention</strong>
          {failures.map(([key, value]) => (
            <div key={key}>
              Post #{key.split(":")[1]}: {value.error}{" "}
              <button onClick={value.retry}>Retry same save</button>
              <button
                onClick={() =>
                  setPending((old) =>
                    Object.fromEntries(
                      Object.entries(old).filter(([k]) => k !== key),
                    ),
                  )
                }
              >
                Dismiss
              </button>
            </div>
          ))}
        </aside>
      )}
    </Context.Provider>
  );
}

export function ReviewButtons({
  project,
  post,
  onSaved,
}: {
  project: number;
  post: Candidate;
  onSaved?: () => void;
}) {
  const reviews = useReviews();
  const pending = reviews.pending[keyFor(project, post.post_id)];
  const state = pending?.saving ? pending.state : post.state;
  return (
    <div className="review-block">
      <div className="review-buttons">
        {(["positive", "negative", "unsure"] as const).map((value, index) => (
          <button
            key={value}
            className={`${value} ${state === value ? "chosen" : ""}`}
            aria-pressed={state === value}
            disabled={pending?.saving || !!pending?.error}
            onClick={() => {
              void reviews.edit(project, [post], value).then((ok) => {
                if (ok) onSaved?.();
              });
            }}
          >
            <kbd>{index + 1}</kbd>{" "}
            {value === "positive"
              ? "Positive"
              : value === "negative"
                ? "Negative"
                : "Unsure"}
          </button>
        ))}
      </div>
      <span className="save-state" aria-live="polite">
        {pending?.saving
          ? "Saving…"
          : pending?.error
            ? "Save failed — retry below"
            : post.state
              ? "Saved"
              : "Unreviewed"}
      </span>
      <button
        className="quiet"
        disabled={!post.event_id || pending?.saving || !!pending?.error}
        onClick={() => {
          void reviews.undo(project, post);
        }}
      >
        Undo this post
      </button>
    </div>
  );
}

function readHistory(): HistoryEntry[] {
  try {
    const value: unknown = JSON.parse(
      sessionStorage.getItem("studio-undo-history") ?? "[]",
    );
    if (!Array.isArray(value)) return [];
    return value
      .filter(
        (entry) =>
          typeof entry?.project === "number" &&
          typeof entry?.result?.event_id === "number" &&
          typeof entry.result.version === "number" &&
          typeof entry.result.post_id === "number",
      )
      .slice(-50);
  } catch {
    return [];
  }
}
