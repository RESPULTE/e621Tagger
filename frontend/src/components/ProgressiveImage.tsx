import { useState } from "react";
import { mediaUrl, type Candidate } from "../api/client";
import { SaveMedia } from "./FavoriteButton";

export function ProgressiveImage({ project, post }: { project: number; post: Candidate }) {
  const [requested, setRequested] = useState(!post.has_sample);
  const [originalReady, setOriginalReady] = useState(false);
  const [sampleFailed, setSampleFailed] = useState(false);
  const [error, setError] = useState(false);
  const showSample = post.has_sample && !sampleFailed && !originalReady;
  return <>
    <SaveMedia project={project} post={post} actualSize={false}>
      {showSample && <img
        src={mediaUrl(post.post_id, "sample", post.md5)}
        alt={`Sample post ${post.post_id}`}
        onError={() => { setSampleFailed(true); setRequested(true); }}
      />}
      {requested && <img
        src={mediaUrl(post.post_id, "original", post.md5)}
        alt={`Original post ${post.post_id}`}
        style={showSample && !originalReady ? { display: "none" } : undefined}
        onLoad={() => setOriginalReady(true)}
        onError={() => setError(true)}
      />}
      {requested && !originalReady && !error && <p className="media-loading" role="status">Loading original image…</p>}
    </SaveMedia>
    {!requested && <button onClick={() => setRequested(true)}>Load original</button>}
    {error && <p className="error" role="alert">Original image could not load. <a href={`https://e621.net/posts/${post.post_id}`} target="_blank" rel="noreferrer">Open the e621 post</a>.</p>}
  </>;
}
