from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database

from app.core.config import get_settings


settings = get_settings()
_client: MongoClient | None = None
_seeded_sequences: set[str] = set()


def _mongo_url() -> str:
    candidate = (settings.MONGODB_URL or settings.DATABASE_URL or "").strip()
    if not candidate.startswith("mongodb"):
        raise RuntimeError("MongoDB URL is not configured. Set MONGODB_URL to your MongoDB Atlas URI.")
    return candidate


def _database_name(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path and parsed.path != "/":
        return parsed.path.lstrip("/")
    return settings.MONGODB_DB_NAME


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(
            _mongo_url(),
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            retryWrites=True,
        )
    return _client


def get_database() -> Database:
    url = _mongo_url()
    return get_client()[_database_name(url)]


def get_collection(name: str) -> Collection:
    return get_database()[name]


def _seed_counter_if_missing(sequence_name: str, collection_name: str) -> None:
    if sequence_name in _seeded_sequences:
        return

    db = get_database()
    counters = db["counters"]
    if counters.find_one({"_id": sequence_name}):
        _seeded_sequences.add(sequence_name)
        return

    top = db[collection_name].find_one(sort=[("id", DESCENDING)])
    max_id = int(top.get("id", 0)) if top and isinstance(top.get("id"), (int, float, str)) else 0
    counters.update_one(
        {"_id": sequence_name},
        {"$setOnInsert": {"value": max_id}},
        upsert=True,
    )
    _seeded_sequences.add(sequence_name)


def get_next_id(sequence_name: str, collection_name: str) -> int:
    _seed_counter_if_missing(sequence_name, collection_name)
    doc = get_database()["counters"].find_one_and_update(
        {"_id": sequence_name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["value"])


def ensure_indexes() -> None:
    db = get_database()
    db["users"].create_index([("id", ASCENDING)], unique=True)
    db["users"].create_index([("email", ASCENDING)], unique=True)
    db["users"].create_index([("email_lookup", ASCENDING)])
    db["users"].create_index([("email_original", ASCENDING)])

    db["assessments"].create_index([("id", ASCENDING)], unique=True)
    db["assessments"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])

    db["payments"].create_index([("id", ASCENDING)], unique=True)
    db["payments"].create_index([("order_id", ASCENDING)], unique=True)
    db["payments"].create_index([("user_id", ASCENDING), ("paid_at", DESCENDING)])
    db["payments"].create_index([("assessment_id", ASCENDING), ("created_at", DESCENDING)])

    db["recommendations"].create_index([("id", ASCENDING)], unique=True)
    db["recommendations"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])

    db["contacts"].create_index([("id", ASCENDING)], unique=True)
    db["contacts"].create_index([("email", ASCENDING), ("created_at", DESCENDING)])
    db["contacts"].create_index([("created_at", DESCENDING)])


def to_public_document(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    data = dict(doc)
    data.pop("_id", None)
    return data
