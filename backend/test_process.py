import pymupdf as fitz
from rag_pipeline import process_pdf
import os
import traceback

def test():
    print("Creating test.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Hello world")
    doc.save("test.pdf")
    doc.close()
    
    print("Running process_pdf")
    try:
        res = process_pdf("test.pdf", document_id="test_doc")
        print("Result:", res)
    except Exception as e:
        print("Error:")
        traceback.print_exc()

if __name__ == "__main__":
    test()
