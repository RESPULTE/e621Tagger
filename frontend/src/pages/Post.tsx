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
import { artistName } from "../components/artworkName";
import { SaveMedia } from "../components/FavoriteButton";

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
  const video = useRef<HTMLVideoElement>(null);
  const ids = navigation.ids ?? [currentId];
  const index = ids.indexOf(currentId);
  const contextParams = new URLSearchParams(location.search);
  const seed = Number(contextParams.get("seed"));
  const indexRevision = contextParams.get("index");
  const searchId = contextParams.get("similar_search");
  const context =
    searchId && indexRevision
      ? { search_id: searchId, index_revision: indexRevision,
          ...(Number.isSafeInteger(seed) && seed > 0 ? { seed_post_id: seed } : {}) }
      : navigation.similarity;
  const post = query.data ? { ...query.data, similarity: context } : undefined;
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
          "input,textarea,select,button,a,video,[contenteditable='true']",
        ) ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        event.repeat ||
        !post
      )
        return;
      const state = (
        { "1": "positive", "2": "negative" } as const
      )[event.key as "1"];
      if (state) {
        event.preventDefault();
        const pending = reviews.pending[`${projectId}:${currentId}`];
        if (!pending?.saving && !pending?.error)
          void reviews.edit(projectId, [post], post.state === state ? null : state);
      } else if (event.key.toLowerCase() === "z") {
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
            <span className="eyebrow">{project.data?.display_name.replaceAll("_", " ") ?? "Artwork"}</span>
            <h1>{artistName(post?.tags.artist)}</h1>
            {post && <p className="artwork-facts"><span>Score <strong>{post.score}</strong></span><span>Rating <strong>{({ s: "Safe", q: "Questionable", e: "Explicit" } as Record<string, string>)[post.rating ?? ""] ?? "Unknown"}</strong></span></p>}
          </div>
          <div className="media-tools">
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
        </header>
        {query.isPending && <p role="status">Loading post…</p>}
        {query.error && (
          <p className="error" role="alert">
            {errorText(query.error)}
          </p>
        )}
        {post && (
          <>
            {post.conflict && <div role="alert" className="error">
              <strong>Conflicting saved reviews</strong>
              <p>This artwork is excluded from new datasets until explicitly resolved.</p>
              {post.conflicting_reviews?.map((review, i) => <p key={i}>Project {String(review.project_id)}: {String(review.state)} (review {String(review.version)})</p>)}
            </div>}
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
              <SaveMedia key={currentId} project={projectId} post={post} actualSize={actualSize}>
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
              </SaveMedia>
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
            <section className="artwork-tags" aria-label="Artwork tags">
              <h2>Tags</h2>
              <TagSidebar tags={post.tags} hidden={false} />
            </section>
            <div className={`detail-review${barHidden ? " is-collapsed" : ""}`} role="group" aria-label="Review artwork" onKeyDownCapture={() => setBarHidden(false)} onFocusCapture={() => setBarHidden(false)}>
              <span className="review-target-name">{project.data?.display_name.replaceAll("_", " ") ?? "Review artwork"}</span>
              <ReviewButtons project={projectId} post={post} />
            </div>
          </>
        )}
      </main>
    </div>
  );
}
