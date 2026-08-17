import React from 'react';
import './LeftSidebar.css';

function LeftSidebar({ onOpenSettings, onNewChat, chats, activeChatId, onSelectChat, onDeleteChat }) {
  // M8 fix: wire up the collapse button to toggle sidebar visibility
  const [collapsed, setCollapsed] = React.useState(false);

  return (
    <div className={`left-sidebar ${collapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-header">
        <div className="logo">
          <span className="logo-text">COGNI</span>
        </div>
        <button className="collapse-btn" onClick={() => setCollapsed(!collapsed)} title={collapsed ? "Expand" : "Collapse"}>
          {collapsed ? '▶' : '◫'}
        </button>
      </div>

      <nav className="sidebar-nav">
        <div className="nav-item active" onClick={onNewChat}>
          <span className="nav-icon">➕</span>
          <span className="nav-label">New Chat</span>
        </div>
        <div className="nav-item" onClick={onOpenSettings}>
          <span className="nav-icon">⚙️</span>
          <span className="nav-label">Settings</span>
        </div>

        <div className="recent-chats-section">
          <div className="recent-chats-header">Recent Chats</div>
          {chats && chats.map((chat) => (
            <div
              key={chat.id}
              className={`recent-chat-item ${activeChatId === chat.id ? 'active' : ''}`}
              onClick={() => onSelectChat(chat.id)}
            >
              <div className="chat-item-content">
                <span className="chat-icon">💬</span>
                <span className="chat-title">{chat.title}</span>
              </div>
              <button
                className="delete-chat-btn"
                onClick={(e) => {
                  e.stopPropagation();
                  onDeleteChat(chat.id);
                }}
                title="Delete chat"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="3 6 5 6 21 6"></polyline>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                  <line x1="10" y1="11" x2="10" y2="17"></line>
                  <line x1="14" y1="11" x2="14" y2="17"></line>
                </svg>
              </button>
            </div>
          ))}
          {(!chats || chats.length === 0) && (
            <div style={{ padding: '0 16px', fontSize: '12px', color: 'var(--text-dark-secondary)' }}>
              No recent chats
            </div>
          )}
        </div>
      </nav>

    </div>
  );
}

export default LeftSidebar;
