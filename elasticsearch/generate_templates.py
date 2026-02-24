#!/usr/bin/env python3
"""Generate ES index template JSON files from schema definitions."""

import argparse
import json
import os

from e2llm_medsynth.locales import load_locale
from e2llm_medsynth.schemas import all_index_configs
from e2llm_medsynth.config import DEFAULT_LOCALE


def main():
    parser = argparse.ArgumentParser(description="Generate ES index templates")
    parser.add_argument("--locale", default=DEFAULT_LOCALE,
                        help="Locale code (default: he_IL)")
    args = parser.parse_args()

    locale = load_locale(args.locale)
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_templates")
    os.makedirs(output_dir, exist_ok=True)

    configs = all_index_configs(locale)
    for idx_name, facility_id, mapping in configs:
        filepath = os.path.join(output_dir, f"{idx_name}.json")

        # Add text_embedding dense_vector field for semantic search
        mapping["mappings"]["properties"]["text_embedding"] = {
            "type": "dense_vector",
            "dims": 1024,
            "index": True,
            "similarity": "cosine",
        }

        template = {
            "index_patterns": [idx_name],
            "template": {
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                },
                "mappings": mapping["mappings"],
            },
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2, ensure_ascii=False)
        print(f"  {filepath}")

    print(f"\nGenerated {len(configs)} index templates.")


if __name__ == "__main__":
    main()
