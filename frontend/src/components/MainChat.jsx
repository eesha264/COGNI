import React, { useState, useRef, useEffect, useCallback } from 'react';
// M3 fix: use centralized API_URL instead of hardcoded 127.0.0.1:8000
const API_URL = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import rehypeKatex from 'rehype-katex';
import rehypeHighlight from 'rehype-highlight';
import { DotLottieReact } from '@lottiefiles/dotlottie-react';
import 'highlight.js/styles/github-dark.css';
import 'katex/dist/katex.min.css';
import './MainChat.css';

// AI responses sometimes contain literal HTML tags (e.g. <br> inside a table cell,
// since markdown tables can't hold real line breaks). rehypeRaw parses that HTML;
// rehypeSanitize then strips anything unsafe (scripts, event handlers, iframes)
// before it's rendered, since this content comes from a model, not a trusted source.
// The math/math-inline/math-display classes are remark-math's markers for content
// rehypeKatex still needs to find and render after sanitizing.
// M4 fix: restrict className to only KaTeX/math class names instead of allowing
// any arbitrary class (which could be used for CSS-based attacks).
const sanitizeSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames || []), 'br'],
  attributes: {
    ...defaultSchema.attributes,
    div: [...(defaultSchema.attributes?.div || []), ['className', /^math|katex|table-scroll-wrapper$/]],
    span: [...(defaultSchema.attributes?.span || []), ['className', /^math|katex$/]],
  },
};

// remark-math only recognizes $...$ / $$...$$ delimiters, but models frequently
// write \(...\) / \[...\] instead regardless of prompt instructions — convert
// those to the delimiters remark-math understands before rendering.
// C16 fix: skip fenced code blocks (```...```) and inline code spans (`...`)
// so that source code containing \[ \] \( \) (regex, shell, LaTeX source) is
// not mangled into math delimiters.
const normalizeLatexDelimiters = (text) => {
  if (!text) return text;

  // Split on fenced code blocks (```...```), preserving the delimiters.
  const fenceSplit = text.split(/(```[\s\S]*?```)/g);
  return fenceSplit.map((segment, i) => {
    // Odd indices (by the capture group) are fenced code blocks — leave them.
    if (i % 2 === 1) return segment;

    // For the non-code segments, also protect inline code spans (`...`).
    const inlineSplit = segment.split(/(`[^`]*`)/g);
    return inlineSplit.map((seg, j) => {
      if (j % 2 === 1) return seg; // inline code — leave it
      return seg
        .replace(/\\\[/g, () => '$$')
        .replace(/\\\]/g, () => '$$')
        .replace(/\\\(/g, () => '$')
        .replace(/\\\)/g, () => '$');
    }).join('');
  }).join('');
};

// Renders AI Markdown responses: headers, bold/italic, tables, ordered/unordered
// lists, blockquotes, links, fenced code blocks with syntax highlighting, and
// LaTeX math ($...$ inline, $$...$$ block) typeset via KaTeX.
const renderMarkdown = (text) => {
  if (!text) return null;
  const normalized = normalizeLatexDelimiters(text);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema], rehypeKatex, rehypeHighlight]}
      components={{
        a: ({ node: _node, ...props }) => (
          <a {...props} target="_blank" rel="noopener noreferrer" />
        ),
        table: ({ node: _node, ...props }) => (
          <div className="table-scroll-wrapper">
            <table {...props} />
          </div>
        ),
      }}
    >
      {normalized}
    </ReactMarkdown>
  );
};

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
  const [copiedId, setCopiedId] = useState(null);
  const [showScrollButton, setShowScrollButton] = useState(false);

  const idCounterRef = useRef(0);
  // L3 fix: use crypto.randomUUID() (available in all modern browsers) for
  // collision-safe IDs across tabs. Fall back to Date.now()+counter+random
  // for older browsers. The old makeId used Date.now()+counter only, which
  // could collide across browser tabs opened in the same millisecond.
  // M6 fix: wrap in useCallback so it's stable and can be safely used in
  // useEffect dependency arrays without stale-closure risk.
  const makeId = useCallback(() => {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return `msg-${crypto.randomUUID()}`;
    }
    return `msg-${Date.now()}-${idCounterRef.current++}-${Math.random().toString(36).slice(2, 8)}`;
  }, []);

  const chatContentRef = useRef(null);
  const bottomRef = useRef(null);
  const autoScrollRef = useRef(true);

  const handleCopy = (content, id) => {
    // M10 fix: clipboard API requires HTTPS or localhost. Fall back to a
    // temporary textarea + execCommand for non-secure contexts.
    const copyToClipboard = (text) => {
      if (navigator.clipboard && (window.isSecureContext || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
        return navigator.clipboard.writeText(text);
      }
      // Fallback for non-secure contexts
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

    copyToClipboard(content).then(() => {
      setCopiedId(id);
      setTimeout(() => setCopiedId((current) => (current === id ? null : current)), 1500);
    }).catch(() => {});
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
      fetch(`${API_URL}/chat/${activeChatId}?device_id=${encodeURIComponent(deviceId)}`)
        .then(res => res.json())
        .then(data => {
          // M7 fix: only generate IDs for messages that don't already have one,
          // so React doesn't remount every chat bubble (which loses copy state
          // and re-runs KaTeX rendering) on every load.
          setMessages((data.messages || []).map((m) => ({ ...m, id: m.id || makeId() })));
        })
        .catch(err => console.error("Error loading chat:", err));
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

      const response = await fetch(`${API_URL}/chat`, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        // M5 fix: don't assume the error response is JSON — the server may
        // return plain text or HTML (e.g. a proxy error page).
        let errDetail;
        try {
          const err = await response.json();
          errDetail = err.detail || err.message || `Server error (${response.status})`;
        } catch {
          errDetail = `Server error (${response.status})`;
        }
        throw new Error(errDetail);
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
                        className="copy-btn"
                        onClick={() => handleCopy(msg.content, msg.id)}
                        title="Copy response"
                        aria-label="Copy response"
                      >
                        {copiedId === msg.id ? '✓' : '⧉'}
                      </button>
                      <div className="markdown-render">
                        {renderMarkdown(msg.content)}
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
            placeholder={isUploading ? "Processing PDF..." : "Ask anything about the uploaded document... (Shift+Enter for newline)"}
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

