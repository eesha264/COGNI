// Central place for the backend's base URL. Defaults to local dev; override at
// build time with a VITE_API_BASE_URL env var (e.g. for a deployed backend).
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';
export const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL || 'ws://127.0.0.1:8000';
