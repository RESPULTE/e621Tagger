import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Tag } from "../api/client";

const categories = [
  "general",
  "artist",
  "contributor",
  "copyright",
  "character",
  "species",
  "invalid",
  "meta",
  "lore",
];
const metatags: Record<string, string[]> = {
  rating: ["s", "q", "e"],
  type: ["jpg", "png", "gif", "webm", "mp4", "swf"],
};

export function QueryInput({
  value,
  onChange,
  label,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const [caret, setCaret] = useState(value.length);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const start = value.slice(0, caret).search(/\S+$/);
  const endMatch = value.slice(caret).search(/\s/);
  const end = endMatch < 0 ? value.length : caret + endMatch;
  const token = start < 0 ? "" : value.slice(start, caret);
  const sign = /^[-~]/.test(token) ? token[0] : "";
  const plain = token.slice(sign.length);
  const colon = plain.indexOf(":");
  const qualifier = colon >= 0 ? plain.slice(0, colon).toLowerCase() : "";
  const term = colon >= 0 ? plain.slice(colon + 1) : plain;
  const tagQuery = colon < 0 || categories.includes(qualifier);
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    setActive(-1);
    const timer = setTimeout(() => setDebounced(term.toLowerCase()), 250);
    return () => clearTimeout(timer);
  }, [term, qualifier]);
  const suggestions = useQuery({
    queryKey: ["autocomplete", debounced],
    enabled:
      open &&
      tagQuery &&
      !!debounced &&
      debounced === term.toLowerCase() &&
      !/[\s*]/.test(debounced) &&
      debounced.length <= 100,
    queryFn: ({ signal }) =>
      api<Tag[]>(`/tags/autocomplete?q=${encodeURIComponent(debounced)}`, {
        signal,
      }),
    staleTime: 60 * 60 * 1000,
    retry: false,
  });
  const choices = tagQuery
    ? debounced === term.toLowerCase()
      ? (suggestions.data ?? [])
      : []
    : (metatags[qualifier] ?? [])
        .filter((item) => item.startsWith(term))
        .map((name) => ({
          canonical_name: name,
          matched_alias: null,
          category: 7,
          post_count: 0,
          tag_id: 0,
        }));
  const shown = open && !!token && choices.length > 0;
  function choose(name: string, retainFocus = true) {
    const replacement =
      sign + (colon >= 0 && !tagQuery ? qualifier + ":" : "") + name;
    const suffix = value.slice(end);
    const next = value.slice(0, start) + replacement + (suffix || " ");
    const position = start + replacement.length + (suffix ? 0 : 1);
    onChange(next);
    setCaret(position);
    setOpen(false);
    requestAnimationFrame(() => {
      if (retainFocus) input.current?.focus();
      input.current?.setSelectionRange(position, position);
    });
  }
  return (
    <div className="query-input">
      <input
        ref={input}
        value={value}
        maxLength={500}
        placeholder={placeholder}
        aria-label={label}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={shown}
        aria-controls={shown ? id : undefined}
        aria-activedescendant={
          shown && active >= 0 ? `${id}-${active}` : undefined
        }
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => {
          onChange(event.target.value);
          setCaret(event.target.selectionStart ?? 0);
          setOpen(true);
        }}
        onSelect={(event) => setCaret(event.currentTarget.selectionStart ?? 0)}
        onKeyUp={(event) => setCaret(event.currentTarget.selectionStart ?? 0)}
        onClick={(event) => setCaret(event.currentTarget.selectionStart ?? 0)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === "Escape") {
            setOpen(false);
            return;
          }
          if (!shown) return;
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setActive(
              (old) =>
                (old +
                  (event.key === "ArrowDown" ? 1 : choices.length - 1) +
                  choices.length) %
                choices.length,
            );
          } else if (
            (event.key === "Enter" || event.key === "Tab") &&
            active >= 0 &&
            choices[active]
          ) {
            if (event.key === "Enter") event.preventDefault();
            choose(choices[active].canonical_name, event.key !== "Tab");
          }
        }}
      />
      {shown && (
        <ul
          id={id}
          role="listbox"
          aria-label="Tag suggestions"
          className="query-suggestions"
        >
          {choices.map((tag, index) => (
            <li
              key={`${tag.canonical_name}:${tag.matched_alias}`}
              id={`${id}-${index}`}
              role="option"
              aria-selected={active === index}
              className={`tag-${categories[tag.category] ?? "general"}`}
              onPointerDown={(event) => event.preventDefault()}
              onClick={() => choose(tag.canonical_name)}
            >
              <span>
                {tag.matched_alias && <small>{tag.matched_alias} → </small>}
                {tag.canonical_name}
              </span>
              {!!tag.post_count && (
                <small>{tag.post_count.toLocaleString()}</small>
              )}
            </li>
          ))}
        </ul>
      )}
      {open && suggestions.isError && tagQuery && (
        <small role="status">
          Suggestions unavailable. You can still search.
        </small>
      )}
    </div>
  );
}
