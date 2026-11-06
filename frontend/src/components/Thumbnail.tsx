import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { mediaUrl } from "../api/client";
import { thumbnailBlob } from "./thumbnailCache";

export function Thumbnail({
  id,
  md5,
  available = true,
  onLoaded,
}: {
  id: number;
  md5?: string | null;
  available?: boolean;
  onLoaded?: () => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["thumbnail", id, md5],
    enabled: available,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    queryFn: ({ signal }) => thumbnailBlob(mediaUrl(id, "preview", md5), signal),
    refetchInterval: (query) => (query.state.data === null ? 1000 : false),
  });
  useEffect(() => {
    const value = query.data ? URL.createObjectURL(query.data) : null;
    setUrl(value);
    return () => {
      if (value) URL.revokeObjectURL(value);
    };
  }, [query.data]);
  if (!available || query.error)
    return <span className="thumbnail-placeholder">Preview unavailable</span>;
  return url ? (
    <img src={url} alt={`Post ${id}`} draggable={false} onLoad={onLoaded} />
  ) : (
    <span className="thumbnail-placeholder">Loading preview…</span>
  );
}
