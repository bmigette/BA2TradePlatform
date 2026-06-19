// Single source of truth for the backend API base URL.
//
// Override per-environment with VITE_API_BASE in frontend/.env.local (or .env), e.g.
//   VITE_API_BASE=http://localhost:8001/api
// No per-file hardcoding — every page/component/lib imports API_BASE from here, so the port
// is changed in ONE place (or, better, via the env var) instead of editing dozens of files.
// Falls back to the default dev port (8000) so a fresh clone works with no configuration.
export const API_BASE: string = import.meta.env.VITE_API_BASE || 'http://localhost:8000/api';
