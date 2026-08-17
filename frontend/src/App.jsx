import React, { useState, useEffect, useRef } from 'react';
import LeftSidebar from './components/LeftSidebar';
import MainChat from './components/MainChat';
import RightSidebar from './components/RightSidebar';
import SettingsModal from './components/SettingsModal';

function App() {
  // C15 fix: store the Groq API key in sessionStorage (cleared when the tab
  // closes) instead of localStorage so it doesn't persist forever and isn't
  // readable by other tabs/origins. device_id stays in localStorage because
  // chat history must persist across sessions.
  const [apiKey, setApiKey] = useState(() => sessionStorage.getItem("groq_api_key") || "");
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [activeStep, setActiveStep] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [deviceId] = useState(() => {
    let id = localStorage.getItem("device_id");
    if (!id) {
      id = "device-" + Math.random().toString(36).substr(2, 9);
      localStorage.setItem("device_id", id);
    }
    return id;
  });
  const [chats, setChats] = useState([]);
  const [activeChatId, setActiveChatId] = useState(null);
  const [resetKey, setResetKey] = useState(0);
  const wsRef = useRef(null);

  const handleSaveApiKey = (key) => {
    sessionStorage.setItem("groq_api_key", key);
    setApiKey(key);
  };

  const startWebSocket = () => {
    // Reset active step
    setActiveStep("Analyzing the pdf");

    // Close existing connection if any
    if (wsRef.current) {
      wsRef.current.close();
    }

    // Connect to backend websocket (C4 fix: send device_id for scoped broadcasts)
    const ws = new WebSocket(`ws://127.0.0.1:8000/ws/process?device_id=${encodeURIComponent(deviceId)}`);
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
      const res = await fetch(`http://127.0.0.1:8000/chats/${deviceId}`);
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
      const response = await fetch(`http://127.0.0.1:8000/chat/${chatId}?device_id=${encodeURIComponent(deviceId)}`, {
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
      const response = await fetch("http://127.0.0.1:8000/upload", {
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
      />
    </div>
  );
}

export default App;
