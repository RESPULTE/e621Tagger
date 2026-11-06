import { useEffect, useState } from "react";
import { useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  api,
  errorText,
  mediaUrl,
  type Candidate,
  type CandidatePage,
  type Project,
} from "../api/client";
import { ReviewButtons, useReviews } from "../components/Reviews";
import { TagSidebar } from "../components/TagSidebar";
import { artistName } from "../components/artworkName";
import { ProgressiveImage } from "../components/ProgressiveImage";
import { AnimatedMedia } from "../components/AnimatedMedia";
import { useMediaSamples, type MediaSample } from "../components/mediaSamples";
import { StudioIcon } from "../components/StudioIcon";
import type { SimilarSearch } from "../components/Similarity";

type NavigationState = {
  ids?: number[];
  gallery?: string;
  similarity?: Candidate["similarity"];
};
export function Post({ canReview }: { canReview: boolean }) {
  const { id, postId } = useParams();
  const projectId = Number(id),
    currentId = Number(postId);
  const location = useLocation();
  const navigation = (location.state ?? {}) as NavigationState;
  const navigate = useNavigate();
  const reviews = useReviews();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["post", projectId, currentId],
    queryFn: ({ signal }) =>
      api<Candidate>(`/projects/${projectId}/candidates/${currentId}`, { signal }),
    gcTime: 5 * 60_000,
    initialData: () => {
      for (const scope of ["feed", "candidates"]) {
        for (const [, data] of client.getQueriesData<InfiniteData<CandidatePage>>({ queryKey: [scope, projectId] })) {
          const candidate = data?.pages.flatMap(page => page.items).find(item => item.post_id === currentId);
          if (candidate) return candidate;
        }
      }
      for (const [, data] of client.getQueriesData<SimilarSearch>({ queryKey: ["similarity", projectId] })) {
        const candidate = data?.results.find(item => item.post_id === currentId)?.candidate;
        if (candidate) return candidate;
      }
      return undefined;
    },
    initialDataUpdatedAt: 0,
  });
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api<Project>(`/projects/${projectId}`),
  });
  const [focused, setFocused] = useState<MediaSample>();
  const [animatedImage, setAnimatedImage] = useState(true);
  const [barHidden, setBarHidden] = useState(false);
  useEffect(() => {
    setBarHidden(false);
    let anchor = window.scrollY;
    const onScroll = () => {
      const y = Math.max(0, window.scrollY);
      if (Math.abs(y - anchor) < 12) return;
      setBarHidden(y > 0 && y < anchor);
      anchor = y;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [currentId]);

  const ids = navigation.ids ?? [currentId];
  const index = ids.indexOf(currentId);
  const contextParams = new URLSearchParams(location.search);
  const seed = Number(contextParams.get("seed"));
  const indexRevision = contextParams.get("index");
  const searchId = contextParams.get("similar_search");
  const context =
    searchId && indexRevision
      ? {
          search_id: searchId,
          index_revision: indexRevision,
          ...(Number.isSafeInteger(seed) && seed > 0
            ? { seed_post_id: seed }
            : {}),
        }
      : navigation.similarity;
  const post = query.data ? { ...query.data, similarity: context } : undefined;
  const samples = useMediaSamples(projectId, post);
  useEffect(() => {
    setFocused((current) => {
      if (!current) return current;
      if (current.post_id !== currentId || current.source_md5 !== post?.md5)
        return undefined;
      const latest = samples.samples.find((item) => item.id === current.id);
      return latest && latest.version > current.version ? latest : current;
    });
  }, [samples.samples, currentId, post?.md5]);
  const isVideo = ["mp4", "webm"].includes(post?.media_ext ?? "");
  const isSampleMedia = isVideo || (animatedImage && ["gif", "webp", "avif"].includes(post?.media_ext ?? ""));
  const imageTypes = ["jpg", "jpeg", "png", "gif", "webp", "avif", "bmp"];
  const isImage = imageTypes.includes(post?.media_ext ?? "");
  const gallery = navigation.gallery ?? `/projects/${projectId}`;
  function move(delta: number) {
    if (index + delta < 0 || index + delta >= ids.length) return;
    navigate(
      `/projects/${projectId}/posts/${ids[index + delta]}${location.search}`,
      {
        state: navigation,
        replace: true,
      },
    );
  }
  useEffect(() => {
    setFocused(undefined);
    setAnimatedImage(true);
  }, [currentId]);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (
        (event.target as HTMLElement).closest(
          "input,textarea,select,button,a,video,[contenteditable='true']",
        ) ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        event.repeat ||
        !post
      )
        return;
      const state = ({ "1": "positive", "2": "negative" } as const)[
        event.key as "1"
      ];
      if (state && canReview) {
        event.preventDefault();
        if (isSampleMedia && !focused) return;
        if (focused) {
          void samples
            .update(focused, { state: focused.state === state ? null : state })
            .then((result) => {
              if (result)
                setFocused((current) =>
                  current?.id === result.id ? result : current,
                );
            });
          return;
        }
        const pending = reviews.pending[`${projectId}:${currentId}`];
        if (!pending?.saving && !pending?.error)
          void reviews.edit(
            projectId,
            [post],
            post.state === state ? null : state,
          );
      } else if (event.key.toLowerCase() === "z" && !focused && canReview) {
        event.preventDefault();
        void reviews.undoLast(projectId, post).then((postId) => {
          if (postId && postId !== currentId) {
            // Undo history spans the project; an unrelated post must not inherit
            // the current folder's image evidence on its next review.
            const inFolder = ids.includes(postId);
            navigate(
              `/projects/${projectId}/posts/${postId}${inFolder ? location.search : ""}`,
              {
                state: inFolder ? navigation : { gallery, ids: [postId] },
                replace: true,
              },
            );
          }
        });
      } else if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        move(event.key === "ArrowRight" ? 1 : -1);
      } else if (event.key === "Escape") navigate(gallery);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  });
  return (
    <div className="workspace detail-layout">
      <main className="main-content post-content">
        <div className="post-navigation">
          <button disabled={index <= 0} onClick={() => move(-1)}>
            ← Previous
          </button>
          <Link to={gallery}>Thumbnail gallery</Link>
          <button
            disabled={index < 0 || index >= ids.length - 1}
            onClick={() => move(1)}
          >
            Next / skip →
          </button>
        </div>
        <header className="artwork-heading">
          <div>
            <span className="eyebrow">
              {project.data?.display_name.replaceAll("_", " ") ?? "Artwork"}
            </span>
            <h1>{artistName(post?.tags.artist)}</h1>
            {post && (
              <p className="artwork-facts">
                <span>
                  Score <strong>{post.score}</strong>
                </span>
                <span>
                  Rating{" "}
                  <strong>
                    {(
                      { s: "Safe", q: "Questionable", e: "Explicit" } as Record<
                        string,
                        string
                      >
                    )[post.rating ?? ""] ?? "Unknown"}
                  </strong>
                </span>
              </p>
            )}
          </div>
        </header>
        {query.isPending && <p role="status">Loading post…</p>}
        {query.error && (
          <p className="error" role="alert">
            {errorText(query.error)}
          </p>
        )}
        {post && (
          <>
            {post.conflict && (
              <div role="alert" className="error">
                <strong>Conflicting saved reviews</strong>
                <p>
                  This artwork is excluded from new datasets until explicitly
                  resolved.
                </p>
                {post.conflicting_reviews?.map((review, i) => (
                  <p key={i}>
                    Project {String(review.project_id)}: {String(review.state)}{" "}
                    (review {String(review.version)})
                  </p>
                ))}
              </div>
            )}
            {!post.has_original || post.unavailable ? (
              <div className="empty-state">
                The original is unavailable. You can still inspect its metadata
                and review history.
              </div>
            ) : !isVideo && !isImage ? (
              <div className="empty-state">
                This media format cannot be displayed here.{" "}
                <a
                  href={`https://e621.net/posts/${currentId}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open the source post
                </a>
                .
              </div>
            ) : isVideo ||
              ["gif", "webp", "avif"].includes(post.media_ext ?? "") ? (
              <AnimatedMedia
                key={`${currentId}:${post.md5}`}
                project={projectId}
                post={post}
                samples={samples.samples}
                focused={focused}
                setFocused={setFocused}
                onAnimationDetected={setAnimatedImage}
                capture={samples.capture}
              />
            ) : (
              <ProgressiveImage
                key={`${currentId}:${post.md5}`}
                project={projectId}
                post={post}
              />
            )}
            {samples.error && (
              <p className="error" role="alert">
                {String(samples.error)}
              </p>
            )}
            <section className="artwork-tags" aria-label="Artwork tags">
              <h2>Tags</h2>
              <TagSidebar projectId={projectId} tags={post.tags} hidden={false} />
            </section>
            {canReview && (!isSampleMedia || focused) && <div
              className={`detail-review${barHidden && !focused ? " is-collapsed" : ""}`}
              role="group"
              aria-label="Review artwork"
              onKeyDownCapture={() => setBarHidden(false)}
              onFocusCapture={() => setBarHidden(false)}
            >
              {focused ? (
                <div className="review-block">
                  <div className="review-buttons">
                    {(["positive", "negative"] as const).map((value) => (
                      <button
                        key={value}
                        className={`${value} ${focused.state === value ? "chosen" : ""}`}
                        aria-label={
                          value === "positive" ? "Positive" : "Negative"
                        }
                        aria-pressed={focused.state === value}
                        onClick={() =>
                          void samples
                            .update(focused, {
                              state: focused.state === value ? null : value,
                            })
                            .then((result) => {
                              if (result)
                                setFocused((current) =>
                                  current?.id === result.id ? result : current,
                                );
                            })
                        }
                      >
                        <StudioIcon name={value} />
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <ReviewButtons project={projectId} post={post} />
              )}
            </div>}
          </>
        )}
      </main>
    </div>
  );
}
