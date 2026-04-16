---
name: meeting-proper-noun-extractor
description: Automatically extract English proper nouns from meeting summary files (.xlsx/.pdf/.docx/.txt/.csv) and save them as a CSV so Whisper corrections and follow-up reviewers can focus on the same vocabulary; trigger this skill whenever you receive a report that lists per-person updates and you need a consolidated list of product/project/brand names (e.g., GenAI Studio, WEDA, Thor, Jetson Thor, OpenClaw, LLMs) without manually curating a dictionary.
---

# Meeting Proper Noun Extractor

## Overview
This skill reads structured meeting summaries, action logs, or status reports and automatically pulls out the English proper nouns that appear. Instead of relying on a closed list of terms, the extractor scans for TitleCase spans and acronyms, filters out templated column headings via a stop list, enforces ASCII-only terms, and writes every detected proper noun (with counts and context windows) to a CSV file for easy Whisper correction.

## Quick Start
1. Drop the report you want to process into the workspace (or reference its absolute path). Supported formats: plain text, PDF, Word, Excel, CSV. When you load a spreadsheet, the script skips structural columns such as `Status`, `Task Type`, `Progress`, or `Comment` so only the descriptive text is scanned.
2. Optional: edit `references/stop-terms.txt` to add any capitalized words that you already know are just labels (e.g., ``Status``, ``Task``, ``New``, ``Feature``). The extractor ignores everything in this file automatically.
3. Run the extractor:
   ```bash
   python scripts/extract_meeting_nouns.py <report-path>
   ```
   Use `--terms-file` if you want to force-capture phrases that do not match the TitleCase/acronym heuristic (for example, a product that mixes lowercase words).
4. The script writes `<report-stem>-proper-nouns.csv` with `term`, `count`, and `contexts` columns. Share that CSV with the Whisper team so they can update their custom vocabulary or double-check misheard names.

## Workflow

### Step 1: Gather the report
Confirm that the report contains the per-person summaries or action-item descriptions. This skill works best on English text where proper nouns follow the usual capitalization conventions.

### Step 2: Tune the stop list (optional but recommended)
The extractor loads `references/stop-terms.txt` to drop false-positive headings and status words. Keep one term per line (comments start with `#`) and update it whenever a new column label shows up in your reports. This keeps the CSV focused on actual proper nouns instead of generic words like `Support` or `Project`.

### Step 3: Run the extractor
Execute `python scripts/extract_meeting_nouns.py <report-path>` (add `--terms-file extra-terms.txt` if you have special phrases). The script handles PDF (`pdfplumber`), Word (`python-docx`), and spreadsheets (`pandas` plus `openpyxl`/`xlrd`). It captures up to three context snippets per term by default; change the `--contexts` flag to adjust.

### Step 4: Share the CSV
Deliver the generated CSV so reviewers can compare the listed terms with Whisper transcripts. If a new proper noun emerges later, add it to the stop list only if you want it ignored in future runs; otherwise the heuristics will keep catching it automatically.

## Scripts

### scripts/extract_meeting_nouns.py
- **Purpose:** Automatically detect English proper nouns (TitleCase spans, acronyms) from documents and export them with counts + context so Whisper reviewers can spot the hard-to-hear vocabulary.
- **Dependencies:** `python3 -m pip install pdfplumber python-docx pandas openpyxl xlrd`
- **Arguments:**
  - `<input>` – required path to the report file
  - `--terms-file` – optional newline-delimited list of extra phrases to capture when the heuristic misses them
  - `--output` – optional path for the CSV (default: `<input-stem>-proper-nouns.csv`)
  - `--contexts` – how many snippets to include per term (default: 3)
- **Outputs:** A CSV with columns `term`, `count`, and `contexts` (context snippets joined by ` | `). The file is safe to share directly with the Whisper team.
- **Notes:** See `references/proper-noun-heuristics.md` for how the extractor determines what counts as a proper noun and how to update the stop list.

## Resources

### references/proper-noun-heuristics.md
Explains the heuristic rules (TitleCase spans + acronyms) and how to manage the stop list or manual overrides using `--terms-file`.

### references/stop-terms.txt
One term per line; the extractor ignores any matches that appear here so column headings, task statuses, and other repeated labels do not pollute the CSV.
