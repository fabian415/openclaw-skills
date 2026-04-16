# Proper noun extraction heuristics

The extractor derives English proper nouns straight from the text instead of relying on a fixed dictionary. When you run `scripts/extract_meeting_nouns.py`, it searches for TitleCase spans (e.g., `GenAI Studio`, `Jetson Thor`, `OpenClaw`) plus acronyms/all-caps phrases (e.g., `WEDA`, `LLM`). After normalizing spacing, trimming punctuation, and ensuring only ASCII terms remain, the script drops any spans that match `references/stop-terms.txt`, limits multi-word matches to at most four words, and only keeps single-word candidates that appear to behave like proper nouns (CamelCase, acronyms, or very short names such as `Thor`, `Alex`). This keeps the CSV focused on English product/project names rather than the ordinary verbs and statuses that often dominate reports.

When processing spreadsheets, the extractor also skips columns whose headers are purely structural (``Status``, ``Task Type``, ``Progress``, ``Comment``, ``Start Date``, ``End Date``, ``Member``) so that only descriptive text is scanned. If your reports use different headers, add them to the column blacklist inside `scripts/extract_meeting_nouns.py` before rerunning so the noise stays low.

## Stop list
`references/stop-terms.txt` contains one term per line and is consulted case-insensitively. Add any new capitalized label, status word, or repeated system term that you do not want the extractor to treat as a proper noun (for example, `Skill`, `Request`, `CPU`, `Version`, `Working Assistant`, `Containerized`).

## Manual overrides
If a phrase never matches the TitleCase/acronym heuristic (for example, it includes lowercase words that are still part of the official name), provide it in a newline-delimited file and pass that file with `--terms-file`. The extractor will hunt for those phrases case-insensitively and append any new matches to the CSV without duplicating spans already captured by the heuristic.

This hybrid approach minimizes noise while keeping the focus on the English proper nouns your Whisper reviewers care about.
