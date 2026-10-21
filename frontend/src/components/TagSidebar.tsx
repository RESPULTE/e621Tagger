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
  tags,
  hidden,
}: {
  tags: Candidate["tags"];
  hidden: boolean;
}) {
  if (hidden) return <p className="muted">Tags hidden while reviewing.</p>;
  return (
    <div className="tag-list">
      {order
        .filter((category) => tags[category]?.length)
        .map((category) => (
          <section key={category}>
            <h3>{category}</h3>
            {tags[category].map((tag) => (
              <a
                className={`tag-${category}`}
                key={tag}
                href={`https://e621.net/posts?tags=${encodeURIComponent(tag)}`}
                target="_blank"
                rel="noreferrer"
              >
                {tag.replaceAll("_", " ")}
              </a>
            ))}
          </section>
        ))}
    </div>
  );
}
