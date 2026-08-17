import React, { useState } from 'react';
import './SettingsModal.css';

function SettingsModal({ isOpen, onClose, apiKey, onSaveApiKey }) {
  const [tempKey, setTempKey] = useState(apiKey);

  if (!isOpen) return null;

  const handleSave = () => {
    onSaveApiKey(tempKey);
    onClose();
  };

  const handleLogout = () => {
    localStorage.removeItem("device_id");
    sessionStorage.removeItem("groq_api_key");
    window.location.reload();
  };

  return (
    <div className="modal-overlay">
      <div className="modal-content fade-in">
        <div className="modal-header">
          <h3>Settings</h3>
          <button className="close-btn" onClick={onClose}>&times;</button>
        </div>
        <div className="modal-body">
          <div className="form-group">
            <label htmlFor="apiKey">Groq API Key</label>
            <input 
              type="password" 
              id="apiKey" 
              placeholder="gsk_..." 
              value={tempKey} 
              onChange={(e) => setTempKey(e.target.value)} 
            />
            <p className="help-text">Your API key is saved in this browser tab's session only (cleared when you close the tab) and is sent only to the backend server to communicate with Groq.</p>
          </div>
        </div>
        <div className="modal-footer" style={{justifyContent: 'space-between'}}>
          <button className="cancel-btn" style={{color: '#ef4444', borderColor: '#ef4444', backgroundColor: 'transparent'}} onClick={handleLogout}>Log out</button>
          <div style={{display: 'flex', gap: '8px'}}>
            <button className="cancel-btn" onClick={onClose}>Cancel</button>
            <button className="save-btn" onClick={handleSave}>Save</button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default SettingsModal;
