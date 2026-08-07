import React, { useRef } from 'react';
import './RightSidebar.css';

const BACKEND = [
  'Analyzing the pdf',   // 0
  'Analyze images',      // 1
  'Analyze tables',      // 2
  'Convert to text',     // 3
  'Create embeddings',   // 4
  'Save to Vector DB',   // 5
  'Done',                // 6
];

function getStatus(idx, activeIndex, isDone) {
  if (isDone)              return 'completed';
  if (idx < activeIndex)   return 'completed';
  if (idx === activeIndex) return 'active';
  return 'pending';
}

function StepCard({ label, icon, status }) {
  return (
    <div className={`step-card ${status}`}>
      {/* Status circle INSIDE the card */}
      <div className="step-circle">
        {status === 'completed' ? (
          <svg className="check-icon" viewBox="0 0 24 24" fill="none">
            <path d="M5 13l4 4L19 7" stroke="currentColor"
              strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        ) : status === 'active' ? (
          <div className="pulse-dot" />
        ) : (
          <div className="idle-dot" />
        )}
      </div>
      <span className="card-icon">{icon}</span>
      <span className="card-label">{label}</span>
    </div>
  );
}

function HLine({ filled, area }) {
  return (
    <div className={`grid-h ga-${area}`}>
      <div className={`h-line ${filled ? 'filled' : ''}`} />
    </div>
  );
}

function VLine({ filled, area }) {
  return (
    <div className={`grid-v ga-${area}`}>
      <div className={`v-line ${filled ? 'filled' : ''}`} />
    </div>
  );
}

function RightSidebar({ activeStep = '', onFileUpload }) {
  const fileInputRef = useRef(null);

  const handleFileChange = (e) => {
    if (e.target.files[0]) onFileUpload(e.target.files[0]);
  };

  const isDone      = activeStep === 'Done';
  const activeIndex = BACKEND.indexOf(activeStep);
  const showGraph   = activeStep !== '';
  const s = BACKEND.map((_, i) => getStatus(i, activeIndex, isDone));

  return (
    <div className="right-sidebar">

      {/* ── STATE 1: Upload ── */}
      {!showGraph && (
        <div className="upload-full">
          <div className="upload-box" onClick={() => fileInputRef.current.click()}>
            <div className="upload-icon-wrap">📄</div>
            <p className="upload-title">Upload PDF Document</p>
            <p className="upload-hint">Up to 400 pages</p>
          </div>
          <input type="file" ref={fileInputRef}
            style={{ display: 'none' }} accept=".pdf"
            onChange={handleFileChange}
          />
        </div>
      )}

      {/* ── STATE 2: Analysis Graph (explicit CSS grid, no reversal tricks) ── */}
      {showGraph && (
        <div className="process-graph">

          {/* ── Row 1 L→R : PDF → Images ── */}
          <div className="ga-c1r1"><StepCard label="PDF"    icon="📄" status={s[0]} /></div>
          <HLine area="h1" filled={s[0]==='completed'} />
          <div className="ga-c3r1"><StepCard label="Images" icon="🖼️" status={s[1]} /></div>

          {/* ── VDrop 1: right side (under Images → enters Tables) ── */}
          <VLine area="vd1" filled={s[1]==='completed'} />

          {/* ── Row 2 R→L : Tables (RIGHT) ← Text (LEFT) ── */}
          <div className="ga-c1r2"><StepCard label="Text"   icon="📝" status={s[3]} /></div>
          <HLine area="h2" filled={s[2]==='completed'} />
          <div className="ga-c3r2"><StepCard label="Tables" icon="📊" status={s[2]} /></div>

          {/* ── VDrop 2: left side (under Text → enters Embed) ── */}
          <VLine area="vd2" filled={s[3]==='completed'} />

          {/* ── Row 3 L→R : Embed → Save ── */}
          <div className="ga-c1r3"><StepCard label="Embed"  icon="🧠" status={s[4]} /></div>
          <HLine area="h3" filled={s[4]==='completed'} />
          <div className="ga-c3r3"><StepCard label="Save"   icon="💾" status={s[5]} /></div>

          {/* ── VDrop 3: right side (under Save → enters Done) ── */}
          <VLine area="vd3" filled={s[5]==='completed'} />

          {/* ── Row 4: Done (centred) ── */}
          <div className="ga-done">
            <StepCard label="Done" icon="✅" status={s[6]} />
          </div>

        </div>
      )}

    </div>
  );
}

export default RightSidebar;
