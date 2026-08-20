import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timezone

# L2 fix: use logging instead of print() for warnings/errors
logger = logging.getLogger("cogni.database")

client = None
db = None
chats_collection = None

# H4 fix: helper to validate chat_id and convert to ObjectId, returning None
# for malformed IDs instead of raising InvalidId (which would cause a 500).
def _to_objectid(chat_id: str):
    if not chat_id:
        return None
    try:
        return ObjectId(chat_id)
    except (InvalidId, TypeError):
        return None

def connect_db():
    global client, db, chats_collection
    mongodb_uri = os.getenv("MONGODB_URI")
    if not mongodb_uri:
        logger.warning("MONGODB_URI not found in environment. Chat history won't be saved.")
        return
    client = AsyncIOMotorClient(mongodb_uri)
    db = client.cogni_db
    chats_collection = db.chats

async def create_chat(device_id: str, initial_message: str, document_id: str = None):
    if chats_collection is None:
        return None

    chat_doc = {
        "device_id": device_id,
        "title": initial_message[:30] + ("..." if len(initial_message) > 30 else ""),
        "created_at": datetime.now(timezone.utc),  # H7 fix: timezone-aware UTC
        "messages": []
    }
    # C8 fix: store document_id on the chat so query_rag can find the right
    # per-document Chroma collection later.
    if document_id:
        chat_doc["document_id"] = document_id
    result = await chats_collection.insert_one(chat_doc)
    return str(result.inserted_id)


async def get_chat_document_id(chat_id: str, device_id: str = None):
    """Return the document_id associated with a chat (for per-doc RAG lookup)."""
    if chats_collection is None:
        return None
    # H4 fix: validate chat_id before constructing ObjectId
    oid = _to_objectid(chat_id)
    if oid is None:
        return None
    try:
        chat = await chats_collection.find_one({"_id": oid})
        if chat:
            if device_id is not None and chat.get("device_id") != device_id:
                return None
            return chat.get("document_id")
    except Exception:
        pass
    return None

async def add_message(chat_id: str, role: str, content: str, tools_used=None, source_pages=None):
    if chats_collection is None:
        return

    # H4 fix: validate chat_id before constructing ObjectId
    oid = _to_objectid(chat_id)
    if oid is None:
        return

    # H7 fix: use timezone-aware UTC instead of deprecated datetime.utcnow()
    message = {"role": role, "content": content, "timestamp": datetime.now(timezone.utc)}
    if tools_used:
        message["tools_used"] = tools_used
    if source_pages:
        message["source_pages"] = source_pages

    try:
        await chats_collection.update_one(
            {"_id": oid},
            {"$push": {"messages": message}}
        )
    except Exception as e:
        logger.error(f"Error adding message to DB: {e}")

async def get_chats(device_id: str, page: int = 1, per_page: int = 50):
    """Get paginated chat list for a device.
    M14 fix: added page/per_page parameters instead of a hard 100-chat ceiling.
    Returns a dict with chats, page, per_page, and has_more for frontend pagination.
    """
    if chats_collection is None:
        return {"chats": [], "page": page, "per_page": per_page, "has_more": False}

    skip = (page - 1) * per_page
    cursor = chats_collection.find({"device_id": device_id}).sort("created_at", -1).skip(skip).limit(per_page + 1)
    chats = await cursor.to_list(length=per_page + 1)

    has_more = len(chats) > per_page
    if has_more:
        chats = chats[:per_page]


    result = []
    for c in chats:
        result.append({
            "id": str(c["_id"]),
            "title": c.get("title", "New Chat"),
            "created_at": c.get("created_at").isoformat() if c.get("created_at") else None
        })
    return {"chats": result, "page": page, "per_page": per_page, "has_more": has_more}

async def get_chat_history(chat_id: str, device_id: str = None):
    if chats_collection is None:
        return []

    # H4 fix: validate chat_id before constructing ObjectId
    oid = _to_objectid(chat_id)
    if oid is None:
        return []

    try:
        chat = await chats_collection.find_one({"_id": oid})
        if chat:
            # C3 fix: ownership check — only the chat's owner can read its history
            if device_id is not None and chat.get("device_id") != device_id:
                return []
            messages = chat.get("messages", [])
            for m in messages:
                if "timestamp" in m:
                    m["timestamp"] = m["timestamp"].isoformat()
            return messages
    except Exception:
        pass
    return []

async def delete_chat(chat_id: str, device_id: str = None):
    if chats_collection is None:
        return False
    # H4 fix: validate chat_id before constructing ObjectId
    oid = _to_objectid(chat_id)
    if oid is None:
        return False
    try:
        # C3 fix: scope deletion by device_id so only the owner can delete
        query = {"_id": oid}
        if device_id is not None:
            query["device_id"] = device_id
        result = await chats_collection.delete_one(query)
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"Error deleting chat: {e}")
        return False
