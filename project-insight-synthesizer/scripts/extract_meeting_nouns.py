#!/usr/bin/env python3
"""Extract English proper nouns from meeting reports."""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_CONTEXT_WINDOW = 40
DEFAULT_MAX_CONTEXTS = 3
PROPER_NOUN_PATTERN = re.compile(r"\b(?:[A-Z][\w&/\.\-]*)(?:\s+(?:[A-Z][\w&/\.\-]*))*\b")
ASCII_TERM_PATTERN = re.compile(r"^[A-Za-z0-9&/\.\- ]+$")
STOP_TERMS_FILE = Path(__file__).resolve().parents[1] / "references" / "stop-terms.txt"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan a report (XLSX/PDF/DOCX/TXT/CSV) and export the English proper nouns it mentions."
    )
    parser.add_argument("input", type=Path, help="Meeting report file path")
    parser.add_argument(
        "--terms-file",
        type=Path,
        help="Optional newline-delimited file of extra phrases to capture (comments with # are ignored)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Destination CSV path (default: <input>-proper-nouns.csv)."
    )
    parser.add_argument(
        "--contexts",
        type=int,
        default=DEFAULT_MAX_CONTEXTS,
        help="How many context snippets to keep per term (default: %(default)s)."
    )
    args = parser.parse_args()

    input_path = args.input
    if not input_path.exists():
        parser.error(f"Input file does not exist: {input_path}")
    try:
        text = load_text(input_path)
    except RuntimeError as exc:
        parser.error(str(exc))

    stop_words = load_stop_terms()
    manual_terms = load_manual_terms(args.terms_file)
    counter, contexts = find_terms(text, manual_terms, stop_words)

    timestamp = Path(input_path).stem
    output_path = (args.output or input_path.with_name(f"{timestamp}-proper-nouns.csv")).resolve()
    write_csv(output_path, counter, contexts, min_contexts=1, max_contexts=args.contexts)

    if counter:
        print(f"Extracted {len(counter)} term(s); saved CSV to {output_path}")
        for term, count in counter.most_common():
            print(f"  {term}: {count} occurrence(s)")
    else:
        print("No English proper nouns were detected; the CSV still contains the header row.")


def load_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf(path)
    if suffix == ".docx":
        return read_docx(path)
    if suffix in {".xls", ".xlsx", ".csv"}:
        return read_spreadsheet(path)
    return read_plain_text(path)


def read_pdf(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("pdfplumber is required to read PDFs (pip install pdfplumber)") from exc

    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts)


def read_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("python-docx is required to read DOCX files (pip install python-docx)") from exc

    doc = Document(path)
    return "\n".join(paragraph.text for paragraph in doc.paragraphs)


def read_spreadsheet(path: Path) -> str:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas (plus openpyxl/xlrd) is required to read spreadsheets") from exc

    suffix = path.suffix.lower()
    read_args = {}
    if suffix == ".csv":
        read_fn = pd.read_csv
        read_args["encoding"] = "utf-8"
    else:
        read_fn = pd.read_excel
    df = read_fn(path, **read_args)

    column_blacklist = {"status", "task type", "progress", "comment", "start date", "end date", "member"}
    columns = [col for col in df.columns if col and str(col).strip().lower() not in column_blacklist]
    if not columns:
        columns = df.columns

    rows: list[str] = []
    for row in df[columns].itertuples(index=False, name=None):
        cells = []
        for value in row:
            if pd.isna(value):
                continue
            snippet = str(value).replace("\n", " ").strip()
            if not snippet:
                continue
            cells.append(snippet)
        if cells:
            rows.append(" ".join(cells))
    return "\n".join(rows)


def read_plain_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def load_stop_terms() -> set[str]:
    if not STOP_TERMS_FILE.exists():
        return set()
    lines = STOP_TERMS_FILE.read_text(encoding="utf-8").splitlines()
    return {line.strip().lower() for line in lines if line.strip() and not line.strip().startswith("#")}


def load_manual_terms(terms_path: Path | None) -> list[str]:
    if not terms_path:
        return []
    if not terms_path.exists():
        raise FileNotFoundError(f"Terms file does not exist: {terms_path}")
    lines = terms_path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def qualifies_as_proper(term: str, stop_words: set[str]) -> bool:
    normalized = term.lower()
    if normalized in stop_words:
        return False
    if not ASCII_TERM_PATTERN.match(term):
        return False
    if not re.search(r"[A-Za-z]", term):
        return False
    if term.count(" ") >= 4:
        return False
    if " " in term:
        return True
    if term.isupper():
        return True
    if any(c.isupper() for c in term[1:]):
        return True
    return len(term) <= 4


def find_terms(text: str, manual_terms: list[str], stop_words: set[str]) -> tuple[Counter[str], dict[str, list[str]]]:
    counter = Counter()
    contexts: dict[str, list[str]] = defaultdict(list)
    seen_spans: set[tuple[int, int]] = set()

    for match in PROPER_NOUN_PATTERN.finditer(text):
        term = normalize_term(match.group(0))
        if not term or len(term) <= 1 or not qualifies_as_proper(term, stop_words):
            continue
        counter[term] += 1
        contexts[term].append(make_snippet(text, match.start(), match.end()))
        seen_spans.add((match.start(), match.end()))

    for term in manual_terms:
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", flags=re.IGNORECASE)
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            if span in seen_spans:
                continue
            canonical = term.strip()
            if not canonical:
                continue
            counter[canonical] += 1
            contexts[canonical].append(make_snippet(text, match.start(), match.end()))
            seen_spans.add(span)

    return counter, contexts


def normalize_term(term: str) -> str:
    trimmed = term.strip()
    trimmed = re.sub(r"[\.\-,:;!?]+$", "", trimmed)
    return re.sub(r"\s+", " ", trimmed)


def make_snippet(text: str, start: int, end: int) -> str:
    begin = max(0, start - DEFAULT_CONTEXT_WINDOW)
    finish = min(len(text), end + DEFAULT_CONTEXT_WINDOW)
    snippet = text[begin:finish].replace("\n", " ").strip()
    return snippet


def write_csv(
    target: Path,
    counter: Counter[str],
    contexts: dict[str, list[str]],
    *,
    min_contexts: int = 1,
    max_contexts: int = DEFAULT_MAX_CONTEXTS,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["term", "count", "contexts"])
        writer.writeheader()
        for term, count in counter.most_common():
            context_list = contexts.get(term, [])[:max_contexts]
            writer.writerow(
                {
                    "term": term,
                    "count": count,
                    "contexts": " | ".join(context_list) if context_list else "",
                }
            )


if __name__ == "__main__":
    main()
