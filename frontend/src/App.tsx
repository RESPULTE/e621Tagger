import {
  Component,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { Link, Route, Routes } from "react-router-dom";
import { api, ApiError, errorText, setCsrf } from "./api/client";
import { ReviewProvider } from "./components/Reviews";
import { Home } from "./pages/Home";
import { Project } from "./pages/Project";
import { Post } from "./pages/Post";

class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: boolean }
> {
  state = { error: false };
  static getDerivedStateFromError() {
    return { error: true };
  }
  render() {
    return this.state.error ? (
      <main className="login-card">
        <h1>This page could not render</h1>
        <p>Your acknowledged labels are saved.</p>
        <button onClick={() => window.location.reload()}>Reload</button>
      </main>
    ) : (
      this.props.children
    );
  }
}

export function App() {
  const [session, setSession] = useState<{ mock: boolean } | null>(null);
  const [checking, setChecking] = useState(true);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [passwordless, setPasswordless] = useState(false);
  const checkingSession = useRef(false);
  async function check() {
    if (checkingSession.current) return;
    checkingSession.current = true;
    setChecking(true);
    setError("");
    try {
      const options = await api<{ password_required: boolean }>("/auth/options");
      setPasswordless(!options.password_required);
      const readSession = () =>
        api<{ csrf_token: string; mock: boolean }>("/auth/session");
      let data;
      try {
        data = await readSession();
      } catch (error) {
        if (
          options.password_required ||
          !(error instanceof ApiError) ||
          error.status !== 401
        )
          throw error;
        await api("/auth/connect", { method: "POST" });
        data = await readSession();
      }
      setCsrf(data.csrf_token);
      setSession(data);
    } catch (error) {
      setSession(null);
      setCsrf("");
      if (!(error instanceof ApiError) || error.status !== 401)
        setError(errorText(error));
    } finally {
      setChecking(false);
      checkingSession.current = false;
    }
  }
  useEffect(() => {
    void check();
    const handler = () => {
      setSession(null);
      setCsrf("");
      void check();
    };
    window.addEventListener("studio-session-expired", handler);
    return () => window.removeEventListener("studio-session-expired", handler);
  }, []);
  async function login(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const data = await api<{ csrf_token: string }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      setCsrf(data.csrf_token);
      setPassword("");
      await check();
    } catch (error) {
      setError(errorText(error));
    } finally {
      setSubmitting(false);
    }
  }
  return (
    <ErrorBoundary>
      <header className="site-header">
        <Link to="/" className="wordmark">
          hydra<span>training studio</span>
        </Link>
        <nav>
          <Link to="/">Projects</Link>
          <span>Discover · Review · Label</span>
        </nav>
        {session && !passwordless && (
          <button
            onClick={() => {
              void api("/auth/logout", { method: "POST" })
                .then(() => {
                  setSession(null);
                  setCsrf("");
                })
                .catch((error) => setError(errorText(error)));
            }}
          >
            Sign out
          </button>
        )}
      </header>
      {checking ? (
        <p className="centered">Checking session…</p>
      ) : !session ? (
        <main className="login-card">
          <p className="eyebrow">YOUR PRIVATE REVIEW WORKSPACE</p>
          <h1>{passwordless ? "Connect to Studio" : "Sign in to Studio"}</h1>
          <p>Search e621. Review the details. Build reliable labels.</p>
          {passwordless ? (
            <button className="primary" onClick={() => void check()}>
              Reconnect
            </button>
          ) : (
            <form onSubmit={login}>
              <label>
                Studio password
                <input
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </label>
              <button className="primary" disabled={submitting}>
                {submitting ? "Signing in…" : "Sign in"}
              </button>
            </form>
          )}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </main>
      ) : (
        <ReviewProvider>
          {session.mock && (
            <div className="demo-banner">
              Demo mode — synthetic images and mock metadata. No requests to
              e621.
            </div>
          )}
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/projects/:id" element={<Project />} />
            <Route path="/projects/:id/posts/:postId" element={<Post />} />
            <Route
              path="*"
              element={
                <main className="centered">
                  <h1>Page not found</h1>
                  <Link to="/">Open projects</Link>
                </main>
              }
            />
          </Routes>
        </ReviewProvider>
      )}
      <footer>
        Hydra Training Studio · Private review workspace · Source tags are
        discovery evidence; your reviews are the labels.
      </footer>
    </ErrorBoundary>
  );
}
