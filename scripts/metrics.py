#!/usr/bin/env python3
"""Compute reproducible "mess metrics" from MedSynth NDJSON corpora.

This is meant for demo/storytelling: quantify schema variance + OCR/format noise
so the impact is measurable (not just claims in the README/video).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


CONCEPT_FIELDS: dict[str, list[str]] = {
    "patient_id": ["patient_id", "tz", "מספר_זהות", "رقم_الهوية", "id", "dni", "curp"],
    "patient_name": ["patient_name", "shem", "שם_מטופל", "name", "الاسم"],
    "age": ["patient_age", "age", "גיל", "age_group", "العمر"],
    "gender": ["gender", "מין", "min", "sex", "الجنس"],
    "document_date": ["document_date", "תאריך", "taarich", "collection_date", "التاريخ"],
    "smoking": ["smoking_status", "smoking"],
    "conditions": ["conditions", "מחלות_רקע", "background", "الأمراض"],
    "diagnosis": ["primary_diagnosis", "אבחנה_ראשית", "diagnosis", "التشخيص"],
    "notes": ["clinical_notes", "סיכום_רפואי", "text", "notes"],
}

RE_AGE_RANGE = re.compile(r"^\s*\d{1,3}\s*-\s*\d{1,3}\s*$")
RE_DATE_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_DATE_ISO_MONTH = re.compile(r"^\d{4}-\d{2}$")
RE_DATE_SLASH = re.compile(r"^\d{2}/\d{2}/\d{4}$")
RE_DATE_DOT_2Y = re.compile(r"^\d{2}\.\d{2}\.\d{2}$")
RE_DIAB_ABBREV = re.compile(r"ס\.\s*ד")


def _facility_from_index(index_name: str) -> str:
    parts = index_name.split("_")
    if len(parts) >= 2 and parts[0] == "medical":
        return parts[1]
    return "unknown"


def _iter_ndjson(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{line_no}: invalid JSON: {e}") from e


def _first_present_key(doc: dict[str, Any], candidates: list[str]) -> str | None:
    for key in candidates:
        if key in doc:
            return key
    return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _flatten_strings(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_flatten_strings(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_flatten_strings(v) for v in value.values())
    return str(value)


def _date_bucket(date_str: str) -> str:
    if RE_DATE_ISO_DAY.match(date_str):
        return "YYYY-MM-DD"
    if RE_DATE_ISO_MONTH.match(date_str):
        return "YYYY-MM"
    if RE_DATE_SLASH.match(date_str):
        return "DD/MM/YYYY"
    if RE_DATE_DOT_2Y.match(date_str):
        return "DD.MM.YY"
    return "other"


@dataclass
class IndexStats:
    index_name: str
    facility: str
    docs: int = 0
    concept_key: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    concept_missing: Counter[str] = field(default_factory=Counter)
    age_range_docs: int = 0
    date_formats: Counter[str] = field(default_factory=Counter)
    id_types: Counter[str] = field(default_factory=Counter)
    diab_canonical: int = 0
    diab_ocr: int = 0
    diab_abbrev: int = 0
    diab_noncanonical_only: int = 0


def compute_stats(data_dir: Path) -> tuple[dict[str, IndexStats], Counter[str]]:
    if not data_dir.exists():
        raise SystemExit(f"Data dir not found: {data_dir}")

    ndjson_files = sorted(data_dir.glob("*.ndjson"))
    if not ndjson_files:
        raise SystemExit(f"No .ndjson files found in: {data_dir}")

    per_index: dict[str, IndexStats] = {}
    totals = Counter()

    for file_path in ndjson_files:
        index_name = file_path.stem
        stats = IndexStats(index_name=index_name, facility=_facility_from_index(index_name))

        for doc in _iter_ndjson(file_path):
            stats.docs += 1

            for concept, candidates in CONCEPT_FIELDS.items():
                key = _first_present_key(doc, candidates)
                if key is None:
                    stats.concept_missing[concept] += 1
                else:
                    stats.concept_key[concept][key] += 1

            age_key = _first_present_key(doc, CONCEPT_FIELDS["age"])
            if age_key is not None:
                age_val = doc.get(age_key)
                if isinstance(age_val, str) and RE_AGE_RANGE.match(age_val):
                    stats.age_range_docs += 1

            date_key = _first_present_key(doc, CONCEPT_FIELDS["document_date"])
            if date_key is not None:
                date_str = _as_text(doc.get(date_key)).strip()
                if date_str:
                    stats.date_formats[_date_bucket(date_str)] += 1

            id_key = _first_present_key(doc, CONCEPT_FIELDS["patient_id"])
            if id_key is not None:
                pid = doc.get(id_key)
                if isinstance(pid, str):
                    stats.id_types["str"] += 1
                    if "." in pid:
                        stats.id_types["str_with_dot"] += 1
                elif isinstance(pid, bool):
                    stats.id_types["bool"] += 1
                elif isinstance(pid, int):
                    stats.id_types["int"] += 1
                elif isinstance(pid, float):
                    stats.id_types["float"] += 1
                else:
                    stats.id_types[type(pid).__name__] += 1

            blob_parts: list[str] = []
            for c in ("conditions", "diagnosis", "notes"):
                key = _first_present_key(doc, CONCEPT_FIELDS[c])
                if key is not None:
                    blob_parts.append(_flatten_strings(doc.get(key)))
            blob = " ".join(blob_parts)

            has_canonical = "סוכרת" in blob
            has_ocr = "סוכדת" in blob
            has_abbrev = bool(RE_DIAB_ABBREV.search(blob))

            if has_canonical:
                stats.diab_canonical += 1
            if has_ocr:
                stats.diab_ocr += 1
            if has_abbrev:
                stats.diab_abbrev += 1
            if (has_ocr or has_abbrev) and not has_canonical:
                stats.diab_noncanonical_only += 1

        per_index[index_name] = stats

        totals["indices"] += 1
        totals["docs"] += stats.docs

    return per_index, totals


def _pct(part: int, whole: int) -> str:
    if whole <= 0:
        return "0%"
    return f"{(part / whole) * 100:.1f}%"


def render_markdown(per_index: dict[str, IndexStats], totals: Counter[str], data_dir: Path) -> str:
    lines: list[str] = []
    lines.append("# Reproducible \"mess metrics\"")
    lines.append("")
    lines.append(f"- Data dir: `{data_dir}`")
    lines.append(f"- Total: **{totals['docs']} docs** across **{totals['indices']} indices**")
    lines.append("")

    lines.append("## Corpus")
    lines.append("")
    lines.append("| Index | Facility | Docs |")
    lines.append("|---|---:|---:|")
    for idx in sorted(per_index):
        s = per_index[idx]
        lines.append(f"| `{s.index_name}` | `{s.facility}` | {s.docs} |")
    lines.append("")

    lines.append("## Schema variance (field aliases)")
    lines.append("")
    lines.append("| Concept | Field names observed |")
    lines.append("|---|---|")
    for concept, candidates in CONCEPT_FIELDS.items():
        observed = Counter()
        for s in per_index.values():
            observed.update(s.concept_key.get(concept, Counter()))
        top = ", ".join([f"`{k}` ({v})" for k, v in observed.most_common(6)]) if observed else "\u2014"
        lines.append(f"| `{concept}` | {top} |")
    lines.append("")

    lines.append("## Format + OCR variance")
    lines.append("")
    total_docs = totals["docs"]

    age_range = sum(s.age_range_docs for s in per_index.values())
    lines.append(f"- Age as range string (e.g. `\"60-70\"`): **{age_range} docs** ({_pct(age_range, total_docs)})")

    date_formats = Counter()
    for s in per_index.values():
        date_formats.update(s.date_formats)
    if date_formats:
        fmt_parts = [f"`{k}`: {v} ({_pct(v, total_docs)})" for k, v in date_formats.most_common()]
        lines.append(f"- Date formats: {', '.join(fmt_parts)}")

    id_types = Counter()
    for s in per_index.values():
        id_types.update(s.id_types)
    if id_types:
        type_parts = [f"`{k}`: {v} ({_pct(v, total_docs)})" for k, v in id_types.most_common()]
        lines.append(f"- Patient ID storage types: {', '.join(type_parts)}")

    diab_can = sum(s.diab_canonical for s in per_index.values())
    diab_ocr = sum(s.diab_ocr for s in per_index.values())
    diab_abbrev = sum(s.diab_abbrev for s in per_index.values())
    diab_noncanon_only = sum(s.diab_noncanonical_only for s in per_index.values())
    lines.append("")
    lines.append("### Diabetes spelling noise (demo-friendly)")
    lines.append("")
    lines.append(f"- Canonical `\u05e1\u05d5\u05db\u05e8\u05ea`: **{diab_can} docs** ({_pct(diab_can, total_docs)})")
    lines.append(f"- OCR typo `\u05e1\u05d5\u05db\u05d3\u05ea`: **{diab_ocr} docs** ({_pct(diab_ocr, total_docs)})")
    lines.append(f"- Abbrev `\u05e1.\u05d3`: **{diab_abbrev} docs** ({_pct(diab_abbrev, total_docs)})")
    lines.append(
        f"- Non-canonical only (would miss with exact keyword match): "
        f"**{diab_noncanon_only} docs** ({_pct(diab_noncanon_only, total_docs)})"
    )

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute reproducible metrics from MedSynth NDJSON corpora")
    parser.add_argument("--data-dir", default="sample_data", help="Directory containing *.ndjson files")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    per_index, totals = compute_stats(data_dir)
    print(render_markdown(per_index, totals, data_dir))


if __name__ == "__main__":
    main()
