import React, { useState, useEffect, useRef } from 'react';
import LeftSidebar from './components/LeftSidebar';
import MainChat from './components/MainChat';
import RightSidebar from './components/RightSidebar';
import SettingsModal from './components/SettingsModal';
import { API_BASE_URL, WS_BASE_URL } from './config';

// crypto.randomUUID() gives 122 bits of entropy and is available in all secure
// contexts (localhost included); fall back only if it's genuinely unavailable.
const generateDeviceId = () => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return 'device-' + crypto.randomUUID();
  }
  return 'device-' + Math.random().toString(36).slice(2, 11);
};

function App() {
  // C15 fix: store the Groq API key in sessionStorage (cleared when the tab
  // closes) instead of localStorage so it doesn't persist forever and isn't
  // readable by other tabs/origins. device_id stays in localStorage because
  // chat history must persist across sessions.
  const [apiKey, setApiKey] = useState(() => sessionStorage.getItem("groq_api_key") || "");
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [activeStep, setActiveStep] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [deviceId, setDeviceId] = useState(() => {
    let id = localStorage.getItem("device_id");
    if (!id) {
      id = generateDeviceId();
      localStorage.setItem("device_id", id);
    }
    return id;
  });
  const [chats, setChats] = useState([]);
  const [activeChatId, setActiveChatId] = useState(null);
  const [resetKey, setResetKey] = useState(0);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const wsRef = useRef(null);

  const handleSaveApiKey = (key) => {
    sessionStorage.setItem("groq_api_key", key);
    setApiKey(key);
  };

  // M9 fix: resets everything in-app instead of a full page reload — a fresh
  // device_id, cleared api key, and an empty chat list. fetchChats() re-runs
  // automatically since it depends on deviceId, confirming the (empty) list
  // for the new id. Clears sessionStorage directly (not just React state) —
  // otherwise a page refresh right after logout would re-read the old key
  // back out of sessionStorage via the apiKey useState initializer above.
  const handleLogout = () => {
    localStorage.removeItem("device_id");
    sessionStorage.removeItem("groq_api_key");
    const newDeviceId = generateDeviceId();
    localStorage.setItem("device_id", newDeviceId);
    setDeviceId(newDeviceId);
    setApiKey("");
    setActiveStep("");
    setChats([]);
    setActiveChatId(null);
    setResetKey(prev => prev + 1);
    setIsSettingsOpen(false);
  };

  const startWebSocket = () => {
    // Reset active step
    setActiveStep("Analyzing the pdf");

    // Close existing connection if any
    if (wsRef.current) {
      wsRef.current.close();
    }

    // Connect to backend websocket (C4 fix: send device_id for scoped broadcasts —
    // the backend now requires it and closes connections that omit it)
    const ws = new WebSocket(`${WS_BASE_URL}/ws/process?device_id=${encodeURIComponent(deviceId)}`);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.step) {
        setActiveStep(data.step);
      } else if (data.error) {
        alert("Error processing PDF: " + data.error);
        setActiveStep("");
      }
    };

    ws.onclose = () => {
      // wsRef.current = null;
    };

    ws.onerror = (err) => {
      console.error("WebSocket Error:", err);
      setActiveStep("");
    };
  };

  const stopWebSocket = () => {
    if (wsRef.current) {
      wsRef.current.close();
    }
  };

  const fetchChats = React.useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/chats/${deviceId}`);
      if (res.ok) {
        const data = await res.json();
        setChats(data.chats || []);
      }
    } catch (err) {
      console.error("Error fetching chats", err);
    }
  }, [deviceId]);

  useEffect(() => {
    fetchChats();
  }, [deviceId, fetchChats]);

  const handleNewChat = () => {
    setActiveChatId(null);
    setActiveStep("");
    setResetKey(prev => prev + 1);
  };

  const handleDeleteChat = async (chatId) => {
    try {
      // C3 fix: pass device_id so the backend can verify ownership before deleting
      const response = await fetch(`${API_BASE_URL}/chat/${chatId}?device_id=${encodeURIComponent(deviceId)}`, {
        method: "DELETE"
      });

      if (response.ok) {
        if (activeChatId === chatId) {
          handleNewChat();
        }
        fetchChats();
      } else {
        console.error("Failed to delete chat.");
      }
    } catch (err) {
      console.error("Error deleting chat:", err);
    }
  };

  const handleFileUpload = async (file) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      alert("Please select a PDF file.");
      return;
    }

    setIsUploading(true);
    startWebSocket();

    const formData = new FormData();
    formData.append("file", file);
    formData.append("device_id", deviceId);
    // C7 fix: send the user's Groq API key so vision OCR works on scanned PDFs
    if (apiKey) {
      formData.append("api_key", apiKey);
    }

    try {
      const response = await fetch(`${API_BASE_URL}/upload`, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        throw new Error("Failed to upload and analyze PDF.");
      }

      const data = await response.json();
      if (data.chat_id) {
        setActiveChatId(data.chat_id);
        fetchChats();
      }
    } catch (err) {
      alert(err.message);
      stopWebSocket();
      setActiveStep("");
    } finally {
      setIsUploading(false);
    }
  };

  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  return (
    <div className="app-container fade-in">
      <LeftSidebar
        onOpenSettings={() => setIsSettingsOpen(true)}
        onNewChat={handleNewChat}
        chats={chats}
        activeChatId={activeChatId}
        onSelectChat={setActiveChatId}
        onDeleteChat={handleDeleteChat}
        isCollapsed={isSidebarCollapsed}
        onToggleCollapse={() => setIsSidebarCollapsed(prev => !prev)}
      />
      <MainChat
        apiKey={apiKey}
        isUploading={isUploading || (activeStep !== "" && activeStep !== "Done")}
        isProcessed={activeStep === "Done"}
        onFileUpload={handleFileUpload}
        deviceId={deviceId}
        activeChatId={activeChatId}
        setActiveChatId={setActiveChatId}
        fetchChats={fetchChats}
        resetKey={resetKey}
      />
      <RightSidebar
        activeStep={activeStep}
        onFileUpload={handleFileUpload}
      />

      <SettingsModal
        isOpen={isSettingsOpen}
        onClose={() => setIsSettingsOpen(false)}
        apiKey={apiKey}
        onSaveApiKey={handleSaveApiKey}
        onLogout={handleLogout}
      />
    </div>
  );
}

export default App;


