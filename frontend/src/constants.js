// M17 fix: shared step labels so backend and frontend don't drift.
// These must match the callback strings in rag_pipeline.py's process_pdf().
export const PROCESS_STEPS = {
  ANALYZING: 'Analyzing the pdf',
  ANALYZE_IMAGES: 'Analyze images',
  ANALYZE_TABLES: 'Analyze tables',
  CONVERT_TEXT: 'Convert to text',
  CREATE_EMBEDDINGS: 'Create embeddings',
  SAVE_VECTOR_DB: 'Save to Vector DB',
  DONE: 'Done',
};

export const STEP_ORDER = [
  PROCESS_STEPS.ANALYZING,
  PROCESS_STEPS.ANALYZE_IMAGES,
  PROCESS_STEPS.ANALYZE_TABLES,
  PROCESS_STEPS.CONVERT_TEXT,
  PROCESS_STEPS.CREATE_EMBEDDINGS,
  PROCESS_STEPS.SAVE_VECTOR_DB,
  PROCESS_STEPS.DONE,
];
