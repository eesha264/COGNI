🧠 COGNI Architecture Analysis
1. The Backend (FastAPI + LangChain + Groq) This is the brain of the application. It acts as a Retrieval-Augmented Generation (RAG) system with a few clever optimizations:

PDF Parsing (rag_pipeline.py): It uses PyMuPDF (fitz) to read uploaded PDFs. It goes page-by-page, extracting text while also tracking image metadata and tables. It rejects PDFs longer than 400 pages.
Local Embeddings: To save on API costs and latency, it uses FastEmbedEmbeddings with the BAAI/bge-small-en-v1.5 model to convert your text into vector embeddings entirely locally.
Vector Database (ChromaDB): The generated embeddings are saved locally to a folder called ./chroma_db. When you ask a question, it searches this database for the top 4 most relevant chunks.
The LLM (Groq): It builds a strict system prompt instructing the AI to only answer based on the document. It connects to the lightning-fast Groq API to generate the final Markdown-formatted response.
Database (database.py): It uses MongoDB (via motor) to save user chat histories. If you don't provide a MONGODB_URI in your .env, the app gracefully skips saving history but continues to function perfectly!
Websockets: When you upload a PDF, the backend uses a WebSocket connection (/ws/process) to stream real-time progress updates back to the UI (e.g., "Analyzing images", "Creating embeddings").
2. The Frontend (React + Vite)

A snappy React application (in frontend/src) that provides the chat UI, handles file uploads, connects to the WebSocket for live loading states, and renders the AI's Markdown responses (including tables and formatting).