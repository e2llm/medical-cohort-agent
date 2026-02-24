#!/usr/bin/env python3
"""Load NDJSON files into Elasticsearch, optionally generating E5 embeddings."""

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from elasticsearch import Elasticsearch, helpers

# Map index prefix → text field name (for embedding)
TEXT_FIELD_MAP = {
    "medical_alon": "clinical_notes",
    "medical_hadarim": "סיכום_רפואי",
    "medical_shaked": "text",
    "medical_ofek": "notes",
}


def _get_text_field(index_name: str) -> str | None:
    """Return the text field name for a given index."""
    for prefix, field in TEXT_FIELD_MAP.items():
        if index_name.startswith(prefix):
            return field
    return None


def embed_batch(texts: list[str], ollama_url: str, model: str) -> list[list[float]]:
    """Call Ollama embedding API for a batch of texts."""
    resp = requests.post(
        f"{ollama_url}/api/embed",
        json={"model": model, "input": texts},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"]


def create_indices(es: Elasticsearch, templates_dir: str, recreate: bool = False):
    """Create indices from template files."""
    templates_path = Path(templates_dir)
    if not templates_path.exists():
        print(f"Templates directory not found: {templates_dir}")
        print("Run: python3 elasticsearch/generate_templates.py")
        sys.exit(1)

    for template_file in sorted(templates_path.glob("*.json")):
        with open(template_file) as f:
            template = json.load(f)

        idx_name = template["index_patterns"][0]
        mappings = template["template"]["mappings"]
        settings = template["template"]["settings"]

        if es.indices.exists(index=idx_name):
            if recreate:
                es.indices.delete(index=idx_name)
                print(f"  Deleted existing index: {idx_name}")
            else:
                print(f"  Index already exists (use --recreate to replace): {idx_name}")
                continue

        es.indices.create(index=idx_name, mappings=mappings, settings=settings)
        print(f"  Created index: {idx_name}")


def bulk_load(es: Elasticsearch, data_dir: str, ollama_url: str | None = None,
              embed_model: str = "") -> dict[str, int]:
    """Bulk load NDJSON files into ES. Returns index_name → doc_count."""
    data_path = Path(data_dir)
    if not data_path.exists():
        print(f"Data directory not found: {data_dir}")
        print("Run: e2llm-medsynth --verbose --output-dir output")
        sys.exit(1)

    counts = {}
    batch_size = 64

    for ndjson_file in sorted(data_path.glob("*.ndjson")):
        idx_name = ndjson_file.stem
        docs = []
        with open(ndjson_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    docs.append(json.loads(line))

        if not docs:
            print(f"  Skipping empty file: {ndjson_file.name}")
            continue

        # Generate embeddings if requested
        if ollama_url:
            text_field = _get_text_field(idx_name)
            if text_field:
                print(f"  Embedding {len(docs)} docs for {idx_name} ({text_field})...", end="", flush=True)
                for i in range(0, len(docs), batch_size):
                    batch = docs[i:i + batch_size]
                    texts = [
                        doc.get(text_field, "") or ""
                        for doc in batch
                    ]
                    # Replace empty strings with a space (embedding API needs non-empty)
                    texts = [t if t.strip() else " " for t in texts]
                    try:
                        vectors = embed_batch(texts, ollama_url, embed_model)
                        for doc, vec in zip(batch, vectors):
                            doc["text_embedding"] = vec
                    except Exception as e:
                        print(f"\n    Warning: embedding batch failed: {e}")
                print(" done")
            else:
                print(f"  No text field mapping for {idx_name} — skipping embeddings")

        actions = [
            {"_index": idx_name, "_source": doc}
            for doc in docs
        ]

        success, errors = helpers.bulk(es, actions, raise_on_error=False)
        if errors:
            print(f"  {idx_name}: {success} indexed, {len(errors)} errors")
            for err in errors[:3]:
                print(f"    {err}")
        else:
            print(f"  {idx_name}: {success} documents indexed")

        counts[idx_name] = success

    return counts


def main():
    # Load .env before argparse so env vars are available for defaults
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Load synthetic medical data into Elasticsearch")
    parser.add_argument("--es-url", default=os.environ.get("ES_URL", "http://localhost:9200"))
    parser.add_argument("--es-user", default=os.environ.get("ES_USER", "elastic"))
    parser.add_argument("--es-password", default=os.environ.get("ELASTIC_PASSWORD", "changeme"))
    parser.add_argument("--data-dir", default="output")
    parser.add_argument("--templates-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "index_templates"))
    parser.add_argument("--skip-create", action="store_true",
                        help="Skip index creation (indices already exist)")
    parser.add_argument("--recreate", action="store_true",
                        help="Delete and recreate existing indices")
    parser.add_argument("--embed", action="store_true",
                        help="Generate text embeddings via Ollama E5")
    parser.add_argument("--ollama-url",
                        default=os.environ.get("OLLAMA_HOST_URL", os.environ.get("OLLAMA_URL", "http://localhost:11434")),
                        help="Ollama API URL for embeddings")
    parser.add_argument("--embed-model",
                        default=os.environ.get("EMBED_MODEL", "qllama/multilingual-e5-large"),
                        help="Ollama embedding model name")
    args = parser.parse_args()

    es = Elasticsearch(
        args.es_url,
        basic_auth=(args.es_user, args.es_password),
        request_timeout=60,
    )

    if not es.ping():
        print(f"Cannot connect to Elasticsearch at {args.es_url}")
        sys.exit(1)

    info = es.info()
    print(f"Connected to Elasticsearch {info['version']['number']}")

    if not args.skip_create:
        print("\nCreating indices...")
        create_indices(es, args.templates_dir, recreate=args.recreate)

    ollama_url = args.ollama_url if args.embed else None
    if ollama_url:
        print(f"\nEmbeddings enabled: {args.embed_model} @ {ollama_url}")

    print("\nBulk loading documents...")
    counts = bulk_load(es, args.data_dir, ollama_url=ollama_url, embed_model=args.embed_model)

    total = sum(counts.values())
    print(f"\nDone. {total} documents across {len(counts)} indices.")


if __name__ == "__main__":
    main()
