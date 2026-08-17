import os
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from datetime import datetime

client = None
db = None
chats_collection = None

def connect_db():
    global client, db, chats_collection
    mongodb_uri = os.getenv("MONGODB_URI")
    if not mongodb_uri:
        print("Warning: MONGODB_URI not found in environment. Chat history won't be saved.")
        return
    client = AsyncIOMotorClient(mongodb_uri)
    db = client.cogni_db
    chats_collection = db.chats

async def create_chat(device_id: str, initial_message: str):
    if chats_collection is None:
        return None
    
    chat_doc = {
        "device_id": device_id,
        "title": initial_message[:30] + ("..." if len(initial_message) > 30 else ""),
        "created_at": datetime.utcnow(),
        "messages": []
    }
    result = await chats_collection.insert_one(chat_doc)
    return str(result.inserted_id)

async def add_message(chat_id: str, role: str, content: str, tools_used=None, source_pages=None):
    if chats_collection is None:
        return

    message = {"role": role, "content": content, "timestamp": datetime.utcnow()}
    if tools_used:
        message["tools_used"] = tools_used
    if source_pages:
        message["source_pages"] = source_pages

    try:
        await chats_collection.update_one(
            {"_id": ObjectId(chat_id)},
            {"$push": {"messages": message}}
        )
    except Exception as e:
        print(f"Error adding message to DB: {e}")

async def get_chats(device_id: str, limit: int = 100):
    if chats_collection is None:
        return []

    cursor = chats_collection.find({"device_id": device_id}).sort("created_at", -1)
    chats = await cursor.to_list(length=limit)
    
    result = []
    for c in chats:
        result.append({
            "id": str(c["_id"]),
            "title": c.get("title", "New Chat"),
            "created_at": c.get("created_at").isoformat() if c.get("created_at") else None
        })
    return result

async def get_chat_history(chat_id: str):
    if chats_collection is None:
        return []
    
    try:
        chat = await chats_collection.find_one({"_id": ObjectId(chat_id)})
        if chat:
            messages = chat.get("messages", [])
            for m in messages:
                if "timestamp" in m:
                    m["timestamp"] = m["timestamp"].isoformat()
            return messages
    except Exception:
        pass
    return []

async def delete_chat(chat_id: str):
    if chats_collection is None:
        return False
    try:
        result = await chats_collection.delete_one({"_id": ObjectId(chat_id)})
        return result.deleted_count > 0
    except Exception as e:
        print(f"Error deleting chat: {e}")
        return False
