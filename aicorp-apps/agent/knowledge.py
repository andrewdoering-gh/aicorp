import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

OLLAMA_BASE_URL = os.environ["OLLAMA_BASE_URL"].rstrip("/")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
COLLECTION = os.environ.get("KNOWLEDGE_COLLECTION", "aicorp-knowledge")
VECTOR_SIZE = int(os.environ.get("EMBEDDING_DIM", "768"))


def request_json(url: str, payload: dict, method: str = "POST") -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code} from {url}: {detail}") from error


def embed(text: str) -> list[float]:
    result = request_json(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        {"model": EMBEDDING_MODEL, "prompt": text},
    )
    return result["embedding"]


def ensure_collection() -> None:
    request = urllib.request.Request(f"{QDRANT_URL}/collections/{COLLECTION}")
    try:
        urllib.request.urlopen(request, timeout=10).close()
        return
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    request_json(
        f"{QDRANT_URL}/collections/{COLLECTION}",
        {"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}},
        method="PUT",
    )


def chunks(text: str, size: int = 1200, overlap: int = 150):
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        yield text[start:end]
        if end == len(text):
            break
        start = end - overlap


def ingest(paths: list[str]) -> None:
    ensure_collection()
    points = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        text = path.read_text(encoding="utf-8")
        for index, chunk in enumerate(chunks(text)):
            point_id = str(uuid.UUID(hashlib.sha256(f"{path}:{index}".encode()).hexdigest()[:32]))
            points.append(
                {
                    "id": point_id,
                    "vector": embed(chunk),
                    "payload": {"source": str(path), "chunk": index, "text": chunk},
                }
            )
    request_json(
        f"{QDRANT_URL}/collections/{COLLECTION}/points?wait=true",
        {"points": points},
        method="PUT",
    )
    print(f"ingested {len(points)} chunks into {COLLECTION}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python knowledge.py ingest FILE [FILE ...]")
    if sys.argv[1] != "ingest":
        raise SystemExit("only the explicit ingest command is supported")
    ingest(sys.argv[2:])
