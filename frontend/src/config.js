// Central place for the backend's base URL. Defaults to local dev; override at
// build time with a VITE_API_BASE_URL env var (e.g. for a deployed backend).
// When deployed (not localhost), automatically use the same origin as the
// frontend so the frontend talks to the Render backend, not the user's machine.
const isLocalhost = typeof window !== 'undefined' &&
  (window.location.hostname === 'localhost' ||
   window.location.hostname === '127.0.0.1' ||
   window.location.hostname === '[::1]');

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ||
  (isLocalhost ? 'http://127.0.0.1:8000' : window.location.origin);

export const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL ||
  (isLocalhost ? 'ws://127.0.0.1:8000' :
    (window.location.protocol === 'https:' ? 'wss://' : 'ws://') + window.location.host);
