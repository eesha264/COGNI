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
  const [wsError, setWsError] = useState(null);  // L2 fix: non-blocking WS error display
  // Multi-PDF fix: how many files are in the current upload batch and which
  // one is currently processing, so the UI can show "File 2 of 3" while a
  // multi-file upload works through the queue.
  const [uploadBatch, setUploadBatch] = useState({ current: 0, total: 0 });
  const wsRef = useRef(null);
  const wsTimeoutRef = useRef(null);  // M24 fix: processing fallback timeout
  // Resolves when the backend broadcasts "Done" (or rejects on "error") for
  // the file currently being processed — lets handleFileUpload await one
  // file's full pipeline before starting the next one in the batch.
  const stepDoneRef = useRef(null);

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
    // Reset active step and clear any previous error
    setActiveStep("Analyzing the pdf");
    setWsError(null);

    // Close existing connection if any
    if (wsRef.current) {
      wsRef.current.close();
    }

    // M24 fix: if the WS drops before "Done" is received, the UI stays stuck
    // in "processing" forever. Set a fallback timeout that clears the state.
    if (wsTimeoutRef.current) {
      clearTimeout(wsTimeoutRef.current);
    }
    wsTimeoutRef.current = setTimeout(() => {
      setActiveStep(prev => {
        // Only clear if still processing (not "Done")
        if (prev && prev !== "Done") {
          setWsError("Processing timed out — the connection may have been lost.");
          return "";
        }
        return prev;
      });
    }, 300000);  // 5 minutes — max expected time for a 400-page scanned PDF

    // Connect to backend websocket (C4 fix: send device_id for scoped broadcasts —
    // the backend now requires it and closes connections that omit it)
    const ws = new WebSocket(`${WS_BASE_URL}/ws/process?device_id=${encodeURIComponent(deviceId)}`);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      // L1 fix: wrap JSON.parse in try/catch — a non-JSON WS message would
      // throw and crash the handler, leaving the UI stuck.
      try {
        const data = JSON.parse(event.data);
        if (data.step) {
          setActiveStep(data.step);
          // M24 fix: clear the fallback timeout when "Done" is received
          if (data.step === "Done" && wsTimeoutRef.current) {
            clearTimeout(wsTimeoutRef.current);
            wsTimeoutRef.current = null;
          }
          // Multi-PDF fix: resolve the pending promise so handleFileUpload's
          // loop moves on to the next file once this one reaches "Done".
          if (data.step === "Done" && stepDoneRef.current) {
            stepDoneRef.current.resolve();
            stepDoneRef.current = null;
          }
        } else if (data.error) {
          // L2 fix: use console.error + state instead of blocking alert()
          console.error("PDF processing error:", data.error);
          setActiveStep("");
          // Still inform the user, but non-blocking
          setWsError(data.error);
          if (stepDoneRef.current) {
            stepDoneRef.current.reject(new Error(data.error));
            stepDoneRef.current = null;
          }
        }
      } catch (e) {
        console.error("Invalid WebSocket message:", e);
      }
    };

    ws.onclose = () => {
      // L3 fix: clear the stale reference so we don't try to close a dead socket
      wsRef.current = null;
      // CRITICAL FIX (Bug #2): if the WebSocket drops while a file is being
      // processed (network blip, proxy timeout, server restart), the pending
      // stepDoneRef promise must be rejected — otherwise handleFileUpload
      // hangs forever inside `await donePromise` with no recovery path.
      if (stepDoneRef.current) {
        stepDoneRef.current.reject(new Error("WebSocket connection closed during processing."));
        stepDoneRef.current = null;
      }
    };

    ws.onerror = (err) => {
      console.error("WebSocket Error:", err);
      setActiveStep("");
      // CRITICAL FIX (Bug #2): same as onclose — reject the pending promise
      // so the upload loop's catch block can recover (alert the user, stop
      // the WebSocket, reset state) instead of hanging forever.
      if (stepDoneRef.current) {
        stepDoneRef.current.reject(new Error("WebSocket error during processing."));
        stepDoneRef.current = null;
      }
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

  // Waits for the backend to broadcast "Done" (or an error) for whichever
  // file is currently processing, so the batch loop below can upload the
  // next file only after the previous one's full pipeline has finished.
  const waitForProcessingDone = () => {
    return new Promise((resolve, reject) => {
      stepDoneRef.current = { resolve, reject };
    });
  };

  // Uploads one file, attaching it to chatId if given (so multiple files
  // land in the same chat instead of each creating a new one). Returns the
  // chat_id from the response — the caller threads this into the next call.
  const uploadOneFile = async (file, chatId) => {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("device_id", deviceId);
    // C7 fix: send the user's Groq API key so vision OCR works on scanned PDFs
    if (apiKey) {
      formData.append("api_key", apiKey);
    }
    // Multi-PDF fix: link this upload to the chat created by the first file
    // in the batch, if any.
    if (chatId) {
      formData.append("chat_id", chatId);
    }

    const response = await fetch(`${API_BASE_URL}/upload`, {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      throw new Error(`Failed to upload and analyze "${file.name}".`);
    }

    const data = await response.json();
    return data.chat_id;
  };

  // Multi-PDF fix: accepts either a single File or an array of Files (the
  // upload box now allows selecting several PDFs at once). Files are
  // uploaded and processed one at a time — each one attached to the chat
  // created for the first file — so the progress graph reflects one file's
  // pipeline at a time instead of interleaving several.
  const handleFileUpload = async (fileOrFiles) => {
    const files = (Array.isArray(fileOrFiles) ? fileOrFiles : [fileOrFiles]).filter(Boolean);
    if (files.length === 0) return;

    // Max 5 PDFs per upload batch — more than that is slow (each file
    // processes sequentially) and hard to manage in one chat.
    const MAX_FILES_PER_UPLOAD = 5;
    if (files.length > MAX_FILES_PER_UPLOAD) {
      alert(`You selected ${files.length} files. Please select at most ${MAX_FILES_PER_UPLOAD} PDF files at a time.`);
      return;
    }

    const nonPdf = files.find(f => !f.name.toLowerCase().endsWith('.pdf'));
    if (nonPdf) {
      alert("Please select only PDF files.");
      return;
    }

    setIsUploading(true);
    setUploadBatch({ current: 0, total: files.length });
    startWebSocket();

    let chatId = null;
    const failedFiles = [];
    try {
      for (let i = 0; i < files.length; i++) {
        setUploadBatch({ current: i + 1, total: files.length });
        setActiveStep("Analyzing the pdf");
        // CRITICAL FIX (Bug #1): set up the "Done" waiter BEFORE sending the
        // HTTP upload request. The backend starts process_pdf in a thread
        // executor *before* the HTTP response returns, so for a fast/small
        // PDF the backend can broadcast {"step":"Done"} via WebSocket while
        // uploadOneFile's fetch is still in flight. If stepDoneRef.current
        // isn't set yet at that point, ws.onmessage silently drops the "Done"
        // message and the promise from waitForProcessingDone never resolves
        // — the UI hangs forever. By creating the promise first, the WebSocket
        // handler can resolve it immediately even if "Done" arrives before
        // the HTTP response does.
        const donePromise = waitForProcessingDone();
        try {
          chatId = await uploadOneFile(files[i], chatId);
          await donePromise;
        } catch (err) {
          // HIGH FIX (Bug #3): a single file failing shouldn't orphan the
          // chat or stop the remaining files from uploading. Record the
          // failure and continue to the next file — the finally block
          // below still activates the chat for whatever files succeeded.
          failedFiles.push(files[i].name);
          console.error(`Upload failed for "${files[i].name}":`, err.message);
          continue;
        }
        // HIGH FIX (Bug #3): set the chat active and refresh the sidebar
        // AFTER each successful file, not just at the end of the loop.
        // If a later file fails, the user still has access to the files
        // that already succeeded — the chat is already active and visible
        // in the sidebar.
        if (chatId) {
          setActiveChatId(chatId);
          fetchChats();
        }
      }

      if (failedFiles.length > 0) {
        const succeeded = files.length - failedFiles.length;
        const fileList = failedFiles.length === 1 ? failedFiles[0] : failedFiles.join('", "');
        const msg = succeeded > 0
          ? `Upload failed for "${fileList}". ${succeeded} file(s) were uploaded successfully and are available in this chat.`
          : `Upload failed for "${fileList}".`;
        alert(msg);
        stopWebSocket();
        setActiveStep("");
      }
    } catch (err) {
      alert(err.message);
      stopWebSocket();
      setActiveStep("");
    } finally {
      // HIGH FIX (Bug #3): even if a file failed mid-batch, activate the
      // chat for whatever files DID succeed — don't leave the user with
      // no way to access them.
      if (chatId) {
        setActiveChatId(chatId);
        fetchChats();
      }
      setIsUploading(false);
      setUploadBatch({ current: 0, total: 0 });
    }
  };

  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
      // M24 fix: clear the fallback timeout on unmount
      if (wsTimeoutRef.current) {
        clearTimeout(wsTimeoutRef.current);
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
        uploadBatch={uploadBatch}
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


