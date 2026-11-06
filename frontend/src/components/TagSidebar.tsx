import { Link } from "react-router-dom";
import type { Candidate } from "../api/client";

const order = [
  "artist",
  "copyright",
  "character",
  "species",
  "general",
  "meta",
  "lore",
  "contributor",
  "invalid",
];
export function TagSidebar({
  projectId,
  tags,
  hidden,
}: {
  projectId: number;
  tags: Candidate["tags"];
  hidden: boolean;
}) {
  if (hidden) return <p className="muted">Tags hidden while reviewing.</p>;
  return (
    <div className="tag-list">
      {[...order, ...Object.keys(tags).filter((category) => !order.includes(category))]
        .filter((category) => tags[category]?.length)
        .map((category) => (
          <section key={category}>
            <h3>{category}</h3>
            {tags[category].map((tag) => (
              <Link
                className={`tag-${category}`}
                key={tag}
                to={`/projects/${projectId}?${new URLSearchParams({ q: tag })}`}
              >
                {tag.replaceAll("_", " ")}
              </Link>
            ))}
          </section>
        ))}
    </div>
  );
}
