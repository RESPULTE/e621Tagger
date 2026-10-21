import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { mediaUrl } from "../api/client";

export function Thumbnail({
  id,
  available = true,
  onLoaded,
}: {
  id: number;
  available?: boolean;
  onLoaded?: () => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["thumbnail", id],
    enabled: available,
    staleTime: Infinity,
    gcTime: 30_000,
    retry: false,
    queryFn: async ({ signal }) => {
      const response = await fetch(mediaUrl(id), { signal });
      if (response.status === 202) return null;
      if (!response.ok) throw new Error("Thumbnail unavailable");
      return response.blob();
    },
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
