import React, { useState, useRef } from 'react';
import { DotLottieReact } from '@lottiefiles/dotlottie-react';
import './MainChat.css';

// A simple Markdown parser to render clean headers, tables, bold text, and bullet points
const renderMarkdown = (text) => {
  if (!text) return null;

  const lines = text.split('\n');
  const elements = [];
  let tableRows = [];
  let inTable = false;
  let inList = false;
  let listItems = [];

  const flushList = (key) => {
    if (listItems.length > 0) {
      elements.push(<ul key={`list-${key}`}>{listItems}</ul>);
      listItems = [];
      inList = false;
    }
  };

  const flushTable = (key) => {
    if (tableRows.length > 0) {
      // Check if first row is header and second is separator
      const hasSeparator = tableRows.length > 1 && tableRows[1].every(cell => cell.trim().startsWith('-'));
      const headerCells = tableRows[0];
      const dataRows = hasSeparator ? tableRows.slice(2) : tableRows.slice(1);

      elements.push(
        <table key={`table-${key}`} className="markdown-table">
          <thead>
            <tr>
              {headerCells.map((cell, idx) => (
                <th key={`th-${idx}`}>{parseInline(cell)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {dataRows.map((row, rowIdx) => (
              <tr key={`tr-${rowIdx}`}>
                {row.map((cell, cellIdx) => (
                  <td key={`td-${cellIdx}`}>{parseInline(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      );
      tableRows = [];
      inTable = false;
    }
  };

  const parseInline = (str) => {
    // Basic bold parser: **text** -> <strong>text</strong>
    const parts = str.split(/\*\*([^*]+)\*\*/g);
    return parts.map((part, idx) => {
      if (idx % 2 === 1) {
        return <strong key={idx}>{part}</strong>;
      }
      return part;
    });
  };

  lines.forEach((line, index) => {
    const trimmed = line.trim();

    // Table Row detection (starts and ends with |)
    if (trimmed.startsWith('|') && trimmed.endsWith('|')) {
      flushList(index);
      inTable = true;
      const cells = trimmed.split('|').slice(1, -1);
      tableRows.push(cells);
      return;
    } else {
      flushTable(index);
    }

    // Bullet list detection
    if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
      inList = true;
      listItems.push(<li key={`li-${index}`}>{parseInline(trimmed.substring(2))}</li>);
      return;
    } else {
      flushList(index);
    }

    // Headers
    if (trimmed.startsWith('### ')) {
      elements.push(<h4 key={index}>{parseInline(trimmed.substring(4))}</h4>);
    } else if (trimmed.startsWith('## ')) {
      elements.push(<h3 key={index}>{parseInline(trimmed.substring(3))}</h3>);
    } else if (trimmed.startsWith('# ')) {
      elements.push(<h2 key={index}>{parseInline(trimmed.substring(2))}</h2>);
    } else if (trimmed) {
      elements.push(<p key={index}>{parseInline(trimmed)}</p>);
    }
  });

  flushList(lines.length);
  flushTable(lines.length);

  return elements;
};

function MainChat({ apiKey, isUploading, isProcessed, onFileUpload, deviceId, activeChatId, setActiveChatId, fetchChats, resetKey }) {
  const [messages, setMessages] = useState([]);
  const [inputValue, setInputValue] = useState('');
  const [isAiTyping, setIsAiTyping] = useState(false);

  React.useEffect(() => {
    if (activeChatId) {
      fetch(`http://127.0.0.1:8000/chat/${activeChatId}`)
        .then(res => res.json())
        .then(data => {
          setMessages(data.messages || []);
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
    setMessages(prev => [...prev, { role: 'user', content: userMessage }]);
    setInputValue('');
    setIsAiTyping(true);

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
      setMessages(prev => [...prev, { role: 'ai', content: data.response }]);

      if (data.chat_id && !activeChatId) {
        setActiveChatId(data.chat_id);
        fetchChats();
      }
    } catch (error) {
      setMessages(prev => [...prev, { role: 'ai', content: `Error: ${error.message}` }]);
    } finally {
      setIsAiTyping(false);
    }
  };



  return (
    <div className="main-chat">
      <div className="chat-content">
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
          messages.map((msg, index) => (
            <div key={index} className={`message-wrapper ${msg.role === 'user' ? 'user-align' : 'ai-align'}`}>
              <div className={`message-bubble ${msg.role === 'user' ? 'user-bubble' : 'ai-bubble'}`}>
                {msg.role === 'user' ? (
                  <p>{msg.content}</p>
                ) : (
                  <div className="markdown-render">
                    {renderMarkdown(msg.content)}
                  </div>
                )}
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
      </div>

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
