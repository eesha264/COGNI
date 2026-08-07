import os
import pymupdf  # fitz
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings

# Initialize the Embeddings
# FastEmbed is lightweight and runs locally without needing an API key for embeddings
embeddings = FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5")

# Initialize Vector Store
vector_store = Chroma(
    collection_name="pdf_docs",
    embedding_function=embeddings,
    persist_directory="./chroma_db"
)

import time

def process_pdf(file_path: str, callback=None):
    """
    Extracts text, tables, and image metadata from a PDF up to 400 pages.
    """
    global vector_store
    if callback:
        callback("Analyzing the pdf")
    time.sleep(1)
    
    doc = pymupdf.open(file_path)
    
    # Check page limit
    if len(doc) > 400:
        return {"error": "PDF exceeds 400 pages limit."}

    if callback:
        callback("Analyze images")
    time.sleep(1)
    
    full_text = ""
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        
        # Extract text (this also often grabs basic table structures reasonably well)
        text = page.get_text("text")
        
        # Get image metadata
        images = page.get_images()
        image_info = f"\n[Page {page_num+1} contains {len(images)} images]\n" if images else ""
        
        full_text += f"\n--- Page {page_num+1} ---\n{text}\n{image_info}"

    if callback:
        callback("Analyze tables")
    time.sleep(1)
    
    # Tables check (Fitz text extraction does tables, but we simulate a deeper analyze step)
    
    if callback:
        callback("Convert to text")
    time.sleep(1)
    
    doc.close()
    
    # Split the document into chunks
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        length_function=len
    )
    
    chunks = text_splitter.split_text(full_text)
    
    # Clear the old vector store collection for a new document upload
    vector_store.delete_collection()
    vector_store = Chroma(
        collection_name="pdf_docs",
        embedding_function=embeddings,
        persist_directory="./chroma_db"
    )
    
    # Add new chunks
    vector_store.add_texts(chunks)
    
    if callback:
        callback("Create embeddings")
    time.sleep(1)
    
    if callback:
        callback("Save to Vector DB")
    time.sleep(1)
    
    if callback:
        callback("Done")
    
    return {"status": "success", "chunks_processed": len(chunks)}


def query_rag(question: str, groq_api_key: str):
    """
    Queries the vector store and returns a response from Groq using the strict system prompt.
    """
    if not groq_api_key:
        return "Error: Groq API key is missing. Please add it to your environment."

    # Retrieve relevant contexts
    results = vector_store.similarity_search(question, k=4)
    context_text = "\n\n".join([doc.page_content for doc in results])
    
    system_prompt = """You are a friendly, helpful, and highly knowledgeable AI assistant integrated into a document analysis system. Your goal is to provide clear, accurate, and easy-to-understand answers to the user's questions.

[INSTRUCTIONS]
1. PRIMARY KNOWLEDGE: Always prioritize the provided document context to answer the user's question. If the context contains the answer, use it as your primary source of truth.
2. GENERAL KNOWLEDGE: If the provided context does not contain the answer, do NOT say "I cannot find the answer". Instead, use your general world knowledge to help the user as best as you can. However, politely let the user know that this specific information was not found in their uploaded document.
3. TONE: Be conversational, polite, and helpful. You can act like a collaborative research partner.
4. FORMATTING: Use natural paragraphs and clear Markdown formatting (headers, bullet points, bold text) to make your response easy to read. Do NOT use Markdown tables unless the user explicitly requests one, or if you are directly extracting a spreadsheet/table from the document.

[CONTEXT DATA]
{context}"""

    prompt_template = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "{input}")
    ])
    
    # Determine model to use (allow override via env var)
    model_name = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    # Initialize Groq LLM
    llm = ChatGroq(
        temperature=0,
        groq_api_key=groq_api_key,
        model_name=model_name,
    )
    
    # Create the chain
    chain = prompt_template | llm
    
    # Invoke
    try:
        response = chain.invoke({
            "context": context_text,
            "input": question
        })
        return response.content
    except Exception as e:
        # Provide a clearer hint for model decommission errors
        err_str = str(e)
        if "decommissioned" in err_str or "model_decommissioned" in err_str:
            return (
                "Error communicating with LLM: the model you are using appears to be decommissioned. "
                "Set the environment variable GROQ_MODEL to a supported model name or visit "
                "https://console.groq.com/docs/deprecations for recommended replacements."
            )
        return f"Error communicating with LLM: {err_str}"
