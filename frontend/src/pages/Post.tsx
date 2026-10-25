import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  api,
  errorText,
  mediaUrl,
  type Candidate,
  type Project,
} from "../api/client";
import { ReviewButtons, useReviews } from "../components/Reviews";
import { TagSidebar } from "../components/TagSidebar";
import { useFindSimilar } from "../components/Similarity";
import { useLongPress } from "../components/useLongPress";
import { FavoriteButton } from "../components/FavoriteButton";

type NavigationState = {
  ids?: number[];
  gallery?: string;
  similarity?: Candidate["similarity"];
};
export function Post() {
  const { id, postId } = useParams();
  const projectId = Number(id),
    currentId = Number(postId);
  const location = useLocation();
  const navigation = (location.state ?? {}) as NavigationState;
  const navigate = useNavigate();
  const reviews = useReviews();
  const query = useQuery({
    queryKey: ["post", projectId, currentId],
    queryFn: () =>
      api<Candidate>(`/projects/${projectId}/candidates/${currentId}`),
  });
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api<Project>(`/projects/${projectId}`),
  });
  const [actualSize, setActualSize] = useState(false);
  const [mediaError, setMediaError] = useState(false);
  const [fallback, setFallback] = useState(false);
  const [mediaReady, setMediaReady] = useState(false);
  const [hideTags, setHideTags] = useState(
    localStorage.getItem("studio-hide-tags") === "true",
  );
  const video = useRef<HTMLVideoElement>(null);
  const ids = navigation.ids ?? [currentId];
  const index = ids.indexOf(currentId);
  const contextParams = new URLSearchParams(location.search);
  const seed = Number(contextParams.get("seed"));
  const indexRevision = contextParams.get("index");
  const context =
    Number.isSafeInteger(seed) && seed > 0 && indexRevision
      ? { seed_post_id: seed, index_revision: indexRevision }
      : navigation.similarity;
  const post = query.data ? { ...query.data, similarity: context } : undefined;
  const similar = useFindSimilar(projectId, currentId);
  const hold = useLongPress(() => void similar.find(), currentId);
  const isVideo = ["mp4", "webm"].includes(post?.media_ext ?? "");
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
    setMediaError(false);
    setFallback(false);
    setActualSize(false);
    setMediaReady(false);
  }, [currentId]);
  useEffect(() => {
    const player = video.current;
    return () => {
      if (player) {
        player.pause();
        player.removeAttribute("src");
        player.load();
      }
    };
  }, [currentId, isVideo, fallback]);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (
        (event.target as HTMLElement).closest(
          "input,textarea,select,video,[contenteditable='true']",
        ) ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        !post
      )
        return;
      const state = (
        { "1": "positive", "2": "negative", "3": "unsure" } as const
      )[event.key as "1"];
      if (state) {
        event.preventDefault();
        void reviews.edit(projectId, [post], state).then((ok) => {
          if (ok) move(1);
        });
      } else if (event.key.toLowerCase() === "z") {
        event.preventDefault();
        void reviews.undoLast(projectId, post).then((postId) => {
          if (postId && postId !== currentId) {
            // Undo history spans the project; an unrelated post must not inherit
            // the current folder's KNN evidence on its next review.
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
      <aside className="sidebar">
        <Link to={gallery}>← Back to posts</Link>
        <p className="review-target">
          Review target
          <br />
          <strong>
            {project.data?.display_name.replaceAll("_", " ") ??
              "Loading target…"}
          </strong>
        </p>
        <h2>Post #{currentId}</h2>
        {post && (
          <>
            {post.conflict && (
              <div role="alert" className="error">
                <strong>Conflicting saved reviews</strong>
                <p>
                  These original decisions were preserved. This post is excluded
                  from new datasets until explicitly resolved.
                </p>
                {post.conflicting_reviews?.map((review, i) => (
                  <p key={i}>
                    Project {String(review.project_id)}: {String(review.state)}{" "}
                    (review {String(review.version)})
                  </p>
                ))}
              </div>
            )}
            <p className="muted">
              {post.width} × {post.height} · {post.media_ext?.toUpperCase()}
              <br />
              Score {post.score} · Rating {post.rating?.toUpperCase()}
            </p>
            <a
              href={`https://e621.net/posts/${currentId}`}
              target="_blank"
              rel="noreferrer"
            >
              View on e621 ↗
            </a>
            <label className="check">
              <input
                type="checkbox"
                checked={hideTags}
                onChange={(event) => {
                  setHideTags(event.target.checked);
                  localStorage.setItem(
                    "studio-hide-tags",
                    String(event.target.checked),
                  );
                }}
              />
              Hide tags
            </label>
            <details className="detail-tags" open>
              <summary>Tags</summary>
              <TagSidebar tags={post.tags} hidden={hideTags} />
            </details>
          </>
        )}
      </aside>
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
        <div className="page-heading">
          <h1>Post #{currentId}</h1>
          {post && (
            <FavoriteButton allowRemoval project={projectId} post={post} />
          )}
          {isImage && (
            <button onClick={() => setActualSize((value) => !value)}>
              {actualSize ? "Fit to window" : "Actual size / zoom"}
            </button>
          )}
          {isVideo && post?.has_video_fallback && (
            <button
              onClick={() => {
                setFallback((value) => !value);
                setMediaError(false);
              }}
            >
              {fallback ? "Use original video" : "Use compatible MP4"}
            </button>
          )}
        </div>
        {query.isPending && <p role="status">Loading post…</p>}
        {query.error && (
          <p className="error" role="alert">
            {errorText(query.error)}
          </p>
        )}
        {post && (
          <>
            <details className="provenance">
              <summary>Discovered through</summary>
              {post.sources.map((source) => (
                <p key={source}>{source}</p>
              ))}
            </details>
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
            ) : (
              <div
                className={`media-stage ${actualSize ? "actual-size" : ""} ${hold.armed ? "similarity-armed" : ""}`}
                tabIndex={0}
                aria-label={`Post ${currentId} media; hold to find similar`}
                onPointerDown={hold.down}
                onPointerMove={hold.move}
                onPointerUp={hold.up}
                onPointerCancel={hold.cancel}
                onPointerLeave={hold.cancel}
                onKeyDown={hold.key}
                onContextMenu={(e) => e.preventDefault()}
              >
                {hold.armed && (
                  <span className="similarity-hint">
                    Release to find similar
                  </span>
                )}
                {isVideo ? (
                  <video
                    ref={video}
                    key={`${currentId}:${fallback}`}
                    controls
                    playsInline
                    preload="metadata"
                    src={mediaUrl(
                      currentId,
                      fallback ? "video_mp4" : "original",
                      post.md5,
                    )}
                    onLoadedMetadata={() => setMediaReady(true)}
                    onError={() => {
                      if (!fallback && post.has_video_fallback) {
                        setFallback(true);
                        setMediaError(false);
                      } else setMediaError(true);
                    }}
                  />
                ) : (
                  <img
                    key={currentId}
                    src={mediaUrl(currentId, "original", post.md5)}
                    alt={`Original post ${currentId}`}
                    onLoad={() => setMediaReady(true)}
                    onError={() => setMediaError(true)}
                  />
                )}
                {!mediaReady && !mediaError && (
                  <p className="media-loading" role="status">
                    Loading original {isVideo ? "video metadata" : "image"}…
                  </p>
                )}
              </div>
            )}
            {mediaError && (
              <p className="error" role="alert">
                Media could not load or this browser does not support its codec.{" "}
                <a
                  href={`https://e621.net/posts/${currentId}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open the e621 post
                </a>
                .
              </p>
            )}
            {similar.notice}
            <div className="detail-review">
              <div>
                <strong>
                  Is{" "}
                  {project.data?.display_name.replaceAll("_", " ") ??
                    "the target"}{" "}
                  visibly present?
                </strong>
              </div>
              <ReviewButtons
                project={projectId}
                post={post}
                onSaved={() => move(1)}
              />
            </div>
          </>
        )}
      </main>
    </div>
  );
}
