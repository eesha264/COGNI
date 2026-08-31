import pymupdf  # L16 fix: use consistent alias (main code uses `import pymupdf`)
from rag_pipeline import process_pdf
import os
import traceback

def test():
    print("Creating test.pdf")
    doc = pymupdf.open()
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
    finally:
        # L31 fix: clean up test artifact
        if os.path.exists("test.pdf"):
            os.remove("test.pdf")

if __name__ == "__main__":
    test()
