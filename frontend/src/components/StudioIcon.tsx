export function StudioIcon({ name }: { name: "positive" | "negative" | "export" | "paw" | "all" }) {
  return <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {name === "all" ? <path strokeWidth="2.2" d="M5 5v8a7 7 0 0 0 14 0V5" /> : name === "positive" ? <path strokeWidth="3.4" d="m5 12 4 4L19 6" /> :
      name === "negative" ? <path strokeWidth="3.4" d="m6 6 12 12M18 6 6 18" /> :
      name === "export" ? <path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5" /> :
      <><ellipse cx="5" cy="9" rx="2" ry="2.8" transform="rotate(-25 5 9)" /><ellipse cx="10" cy="5" rx="2" ry="2.8" /><ellipse cx="16" cy="6" rx="2" ry="2.8" transform="rotate(20 16 6)" /><ellipse cx="20" cy="11" rx="1.8" ry="2.5" transform="rotate(30 20 11)" /><path d="M6 17c0-3 4-6 6-6s6 4 6 7c0 4-4 1-6 1s-6 2-6-2Z" /></>}
  </svg>;
}
