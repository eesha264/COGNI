import React, { useState, useRef, useEffect, useCallback, Suspense, lazy } from 'react';
import { DotLottieReact } from '@lottiefiles/dotlottie-react';
import { API_BASE_URL } from '../config';
import './MainChat.css';

// Lazily loaded — react-markdown + remark/rehype plugins + katex + highlight.js
// are heavy (pushed the bundle past 500KB) and aren't needed until an AI message
// actually needs rendering, so they're split into their own chunk instead of
// always being part of the initial bundle (e.g. for the empty state). M4's
// className restriction and C16's code-block-aware LaTeX normalization both
// live inside MarkdownRenderer.jsx now, not inline here.
const MarkdownRenderer = lazy(() => import('./MarkdownRenderer'));

const formatTimestamp = (ts) => {
  if (!ts) return '';
  const date = typeof ts === 'string' ? new Date(ts) : ts;
  if (isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
};

function MainChat({ apiKey, isUploading, isProcessed, deviceId, activeChatId, setActiveChatId, fetchChats, resetKey }) {
  const [messages, setMessages] = useState([]);
  const [inputValue, setInputValue] = useState('');
  const [isAiTyping, setIsAiTyping] = useState(false);
  const [copyState, setCopyState] = useState(null); // { id, status: 'success' | 'failed' }
  const [showScrollButton, setShowScrollButton] = useState(false);

  const idCounterRef = useRef(0);
  // L3 fix: use crypto.randomUUID() (available in all modern browsers) for
  // collision-safe IDs across tabs. Fall back to Date.now()+counter+random
  // for older browsers. The old makeId used Date.now()+counter only, which
  // could collide across browser tabs opened in the same millisecond.
  // M6 fix: wrapped in useCallback so it's stable and can be safely used in
  // useEffect dependency arrays without stale-closure risk.
  const makeId = useCallback(() => {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return `msg-${crypto.randomUUID()}`;
    }
    return `msg-${Date.now()}-${idCounterRef.current++}-${Math.random().toString(36).slice(2, 8)}`;
  }, []);

  const chatContentRef = useRef(null);
  const bottomRef = useRef(null);
  const textareaRef = useRef(null);
  const MAX_TEXTAREA_HEIGHT = 160;

  // Auto-grow the input as the user types multi-line messages, up to a max
  // height, beyond which it scrolls internally instead of growing forever.
  // Deferred to the next animation frame because measuring scrollHeight
  // immediately (e.g. on first mount) can race the flex layout still settling,
  // reading a near-zero width and wrapping the placeholder into dozens of lines
  // — that bad measurement then gets locked in since this effect only re-runs
  // on inputValue changes, not once layout stabilizes a moment later.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    const raf = requestAnimationFrame(() => {
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT) + 'px';
    });
    return () => cancelAnimationFrame(raf);
  }, [inputValue]);
  const autoScrollRef = useRef(true);

  const showCopyState = (id, status) => {
    setCopyState({ id, status });
    setTimeout(() => setCopyState((s) => (s?.id === id ? null : s)), 1500);
  };

  const handleCopy = (content, id) => {
    // navigator.clipboard is undefined in non-secure contexts (plain HTTP on a
    // non-localhost origin), and refuses to run outside a secure context even
    // when the object exists on some browsers. M10 fix: fall back to a
    // temporary textarea + execCommand so copying still works there, instead
    // of just failing gracefully.
    const copyToClipboard = (text) => {
      if (navigator.clipboard && (window.isSecureContext || location.hostname === 'localhost' || location.hostname === '127.0.0.1' || location.hostname === '[::1]' || location.hostname === '::1')) {
        return navigator.clipboard.writeText(text);
      }
      return new Promise((resolve, reject) => {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        try {
          document.execCommand('copy');
          resolve();
        } catch (e) {
          reject(e);
        } finally {
          document.body.removeChild(textarea);
        }
      });
    };

    copyToClipboard(content)
      .then(() => showCopyState(id, 'success'))
      .catch(() => showCopyState(id, 'failed'));
  };

  const scrollToBottom = (behavior = 'auto') => {
    bottomRef.current?.scrollIntoView({ behavior });
    setShowScrollButton(false);
    autoScrollRef.current = true;
  };

  const handleScroll = () => {
    const el = chatContentRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const nearBottom = distanceFromBottom < 80;
    autoScrollRef.current = nearBottom;
    setShowScrollButton(!nearBottom);
  };

  // Auto-scroll on new messages / typing indicator, unless the user has
  // scrolled up to read earlier content
  useEffect(() => {
    if (autoScrollRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'auto' });
    }
  }, [messages, isAiTyping]);

  React.useEffect(() => {
    if (activeChatId) {
      // M23 fix: use AbortController so a stale fetch (e.g. user switched
      // to another chat before the first one loaded) is aborted and can't
      // overwrite the correct chat's messages.
      const controller = new AbortController();
      fetch(`${API_BASE_URL}/chat/${activeChatId}?device_id=${encodeURIComponent(deviceId)}`, {
        signal: controller.signal,
      })
        .then(res => res.json())
        .then(data => {
          setMessages((data.messages || []).map((m, idx) => ({
            ...m,
            id: m.timestamp ? `hist-${m.timestamp}-${idx}` : makeId(),
          })));
        })
        .catch(err => {
          if (err.name !== 'AbortError') {
            console.error("Error loading chat:", err);
          }
        });
      return () => controller.abort();
    } else {
      setMessages([]);
    }
  }, [activeChatId, resetKey, deviceId, makeId]);

  const handleSend = async () => {
    if (!inputValue.trim() || isAiTyping) return;

    if (!apiKey) {
      alert("Please configure your Groq API Key in Settings first!");
      return;
    }

    const userMessage = inputValue;
    setMessages(prev => [...prev, { role: 'user', content: userMessage, timestamp: new Date().toISOString(), id: makeId() }]);
    setInputValue('');
    setIsAiTyping(true);
    autoScrollRef.current = true;

    try {
      const formData = new FormData();
      formData.append("message", userMessage);
      formData.append("api_key", apiKey);
      formData.append("device_id", deviceId);
      if (activeChatId) {
        formData.append("chat_id", activeChatId);
      }

      const response = await fetch(`${API_BASE_URL}/chat`, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        // M5 fix: don't assume the error response is JSON — the server may
        // return plain text or HTML (e.g. a proxy error page).
        let message = `Failed to get AI response (server returned ${response.status})`;
        try {
          const err = await response.json();
          message = err.detail || err.message || message;
        } catch {
          // Response body wasn't JSON (e.g. a proxy's HTML error page) — keep the generic message
        }
        throw new Error(message);
      }

      const data = await response.json();
      setMessages(prev => [...prev, {
        role: 'ai',
        content: data.response,
        source_pages: data.source_pages,
        timestamp: new Date().toISOString(),
        id: makeId(),
      }]);

      if (data.chat_id && !activeChatId) {
        setActiveChatId(data.chat_id);
        fetchChats();
      }
    } catch (error) {
      setMessages(prev => [...prev, { role: 'ai', content: `Error: ${error.message}`, timestamp: new Date().toISOString(), id: makeId() }]);
    } finally {
      setIsAiTyping(false);
    }
  };



  return (
    <div className="main-chat">
      <div className="chat-content" ref={chatContentRef} onScroll={handleScroll}>
        {messages.length === 0 ? (
          <div className="empty-state">
            {!isUploading ? (
              <>
                {isProcessed ? (
                  <>
                    <h2>Document Analysis Complete</h2>
                    <p>Your document has been successfully processed! You can now ask any question about the contents of the document.</p>
                  </>
                ) : (
                  <>
                    <h2>Cogni Document RAG</h2>
                    <p>Upload a document (up to 400 pages) and ask questions. Cogni will extract text, analyze tables and images, and answer using the strict accuracy guidelines.</p>
                  </>
                )}
              </>
            ) : (
              <div className="upload-animation">
                <DotLottieReact
                  src="https://lottie.host/b81edea4-303c-467f-af87-5e16a7577dca/cca2bEg7Z8.lottie"
                  loop
                  autoplay
                  style={{ width: '250px', height: '250px', margin: '0 auto' }}
                />
                <p className="upload-notice" style={{ marginTop: '20px', fontWeight: 'bold' }}>⚡ File processing started. Follow progress in the right sidebar.</p>
              </div>
            )}
          </div>
        ) : (
          messages.map((msg) => (
            <div key={msg.id} className={`message-wrapper ${msg.role === 'user' ? 'user-align' : 'ai-align'}`}>
              <div className="message-group">
                <div className={`message-bubble ${msg.role === 'user' ? 'user-bubble' : 'ai-bubble'}`}>
                  {msg.role === 'user' ? (
                    <p>{msg.content}</p>
                  ) : (
                    <>
                      <button
                        className={`copy-btn ${copyState?.id === msg.id && copyState.status === 'failed' ? 'copy-failed' : ''}`}
                        onClick={() => handleCopy(msg.content, msg.id)}
                        title={copyState?.id === msg.id && copyState.status === 'failed' ? 'Copy failed — clipboard unavailable' : 'Copy response'}
                        aria-label="Copy response"
                      >
                        {copyState?.id === msg.id ? (copyState.status === 'success' ? '✓' : '✕') : '⧉'}
                      </button>
                      <div className="markdown-render">
                        <Suspense fallback={<p>{msg.content}</p>}>
                          <MarkdownRenderer text={msg.content} />
                        </Suspense>
                      </div>
                      {msg.source_pages && msg.source_pages.length > 0 && (
                        <div className="source-footer">
                          📄 Source{msg.source_pages.length > 1 ? 's' : ''}: {msg.source_pages.map(p => `Page ${p}`).join(', ')}
                        </div>
                      )}
                    </>
                  )}
                </div>
                {msg.timestamp && <div className="message-timestamp">{formatTimestamp(msg.timestamp)}</div>}
              </div>
            </div>
          ))
        )}
        {isAiTyping && (
          <div className="message-wrapper ai-align">
            <div className="message-bubble ai-bubble typing-indicator">
              <span></span>
              <span></span>
              <span></span>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {showScrollButton && (
        <button className="scroll-to-bottom-btn" onClick={() => scrollToBottom()}>
          ↓ Scroll to bottom
        </button>
      )}

      <div className="chat-input-container">
        <div className="input-wrapper">
          {/* M16 fix: use a textarea so Shift+Enter creates a newline and
              Enter sends. The old single-line input couldn't do multi-line. */}
          <textarea
            ref={textareaRef}
            placeholder={isUploading ? "Processing PDF..." : "Ask anything about the uploaded document... (Shift+Enter for a new line)"}
            className="chat-input"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            disabled={isUploading}
            rows={1}
          />
          <button className="send-btn" onClick={handleSend} disabled={isUploading || isAiTyping}>➤</button>
        </div>
        <p className="disclaimer">Document Analysis AI accurately extracts data from your documents.</p>
      </div>
    </div>
  );
}

export default MainChat;

