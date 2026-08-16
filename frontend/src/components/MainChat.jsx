import React, { useState, useRef, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { DotLottieReact } from '@lottiefiles/dotlottie-react';
import 'highlight.js/styles/github-dark.css';
import './MainChat.css';

// Renders AI Markdown responses: headers, bold/italic, tables, ordered/unordered
// lists, blockquotes, links, and fenced code blocks with syntax highlighting.
const renderMarkdown = (text) => {
  if (!text) return null;
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={{
        a: ({ node: _node, ...props }) => (
          <a {...props} target="_blank" rel="noopener noreferrer" />
        ),
      }}
    >
      {text}
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
  const makeId = () => `msg-${Date.now()}-${idCounterRef.current++}`;

  const chatContentRef = useRef(null);
  const bottomRef = useRef(null);
  const autoScrollRef = useRef(true);

  const handleCopy = (content, id) => {
    navigator.clipboard.writeText(content).then(() => {
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
      fetch(`http://127.0.0.1:8000/chat/${activeChatId}`)
        .then(res => res.json())
        .then(data => {
          setMessages((data.messages || []).map((m) => ({ ...m, id: makeId() })));
        })
        .catch(err => console.error("Error loading chat:", err));
    } else {
      setMessages([]);
    }
  }, [activeChatId, resetKey]);

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

      const response = await fetch("http://127.0.0.1:8000/chat", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || "Failed to get AI response");
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
          <input
            type="text"
            placeholder={isUploading ? "Processing PDF..." : "Ask anything about the uploaded document..."}
            className="chat-input"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSend()}
            disabled={isUploading}
          />
          <button className="send-btn" onClick={handleSend} disabled={isUploading || isAiTyping}>➤</button>
        </div>
        <p className="disclaimer">Document Analysis AI accurately extracts data from your documents.</p>
      </div>
    </div>
  );
}

export default MainChat;
