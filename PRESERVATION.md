> Veil 1.0.0: as selected for this deployment, saved PPTX printer settings are
> removed from redacted copies. Slides, slide layouts, notes and comments retain
> the existing preservation behavior. Relationships and content-type entries for
> removed settings are cleaned up, and reports record the removal as information.
> Word/Excel printer settings remain subject to their existing review behavior.

# Preservation mode in Veil 0.3

The original brief requested removal of comments/revision remnants by default and a conservative formula policy. Veil's earlier implementation additionally removed every Excel formula/cache and Word field instruction. That blanket formula deletion was an implementation shortcut, not an external or organizational requirement. The current preservation preference supersedes those earlier defaults.

The default is now `veil-preserve-content-v4`: preserve supported content, redact detected PII in place, and report changes. There is no default stripping mode. Original uploads and earlier job results are never overwritten; resubmit an original to create a new result under this profile.

## What stays

- Ordinary text, tables, styles and non-PII content.
- Word comments/anchors, inserted and deleted text, moves, IDs, timestamps and tracked-formatting structures. Both original and final revision views are inspected. Author PII and identifying initials are sanitised without discarding the history entry.
- PowerPoint classic comments/authors, relationships, notes, hidden status and layouts. Modern/extended schemas require review or are blocked when unsupported.
- Excel comments and their VML drawing parts; hidden sheets/rows/columns; defined names; supported formula expressions and benign cached results.
- Benign Office metadata, thumbnails and normal hyperlinks. Thumbnails are OCR-inspected and require review. Printer-settings binaries are retained unchanged, identified as uninspected, and require review; their contents may retain PII. They are never executed.

## Formulas

Veil tokenizes and inspects formula syntax; it does not calculate formulas, invoke Excel/LibreOffice, execute macros, fetch external data or run a formula evaluator during redaction.

Examples:

| Input | Behavior |
|---|---|
| `=SUM( A1,   B1 )` with benign data/cache | Formula, spacing, number format and cached total remain. |
| `="private@example.test"` | Replace only the sensitive literal, such as `="[EMAIL_ADDRESS_001]"`; retain the formula. |
| `=A1` where A1 is redacted | Keep the formula; clear stale dependent caches and flag recalculation/review. Transitive and supported named-range dependencies are included. |
| A comment containing an email plus ordinary instructions | Preserve the comment and instructions, redact the email. |
| `=CHAR(...)`, concatenation, `INDIRECT`, external references, unsupported array/spill/table expressions | Block intact processing with an explanation and no partial sanitised artifact. Never silently delete these formulas. |

Supported formula operations include common arithmetic, comparisons, local cell/range/name references, SUM/AVERAGE/MIN/MAX/COUNT families, IF/IFERROR and an explicit numeric-function whitelist in `veil/office_preservation.py`. Normal and supported shared formulas retain their structure. Name identifiers and references are preserved; sensitive worksheet identifiers requiring coordinated renaming currently block the job. Missing cached results require review because results were not evaluated.

Only affected caches are invalidated; unrelated cached totals remain. Spreadsheet recalculation flags are set when necessary without changing an existing manual calculation mode. Formula arguments containing PII can change calculation semantics after redaction; reports identify those cells. Some sensitive formula outputs cannot be safely sanitised while preserving the calculation and block output. No claim is made that all spreadsheet functionality is preserved or that a future recalculation can never expose missed PII.

## Word fields and history

Page/section/date/time fields with supported formatting remain. Unsupported data-bound, executable, external or unbalanced field instructions block output rather than being deleted. This is a limited field implementation, not comprehensive Word field evaluation.

When a formatting run belongs to both old and new revision views, both views are analysed before edits are applied. Only detected PII character ranges are changed. Shared spans that represent different identities are flagged for review; Veil does not promise perfect identity correspondence across complex revisions. Entity counts can include the same physical text in both logical revision views.

## Reports and limits

`policy` means a versioned Veil implementation profile. It does not assert an outside authority. Reports include preservation mode, detected spans, `content_changes`, per-item `content_adjustments`, review tasks, informational notices and `uninspected_content`; ordinary reports do not contain original values or formula text. Retaining unsupported opaque content always requires review. Unsupported executable/embedded content remains blocked.

Preservation is structural and practical, not byte-for-byte: ZIP/XML serialization, redacted runs, OCR image encoding and CJK fallback fonts can change. Model false positives may redact benign text; model misses remain possible. Old benchmark numbers apply to their original profiles and are not reasserted for this release.

The existing **PDF pipeline still flattens pages into sanitised images**, losing searchable text, annotations/forms, signatures and original object structure. This change fixes Office preservation; it does not make full-fidelity PDF preservation implemented. The UI and report must not be read as that guarantee.

Run tests with `.venv/bin/python -m pytest -q`. Start/update only Veil with `docker compose up -d --build`; stop with `docker compose down`. Live UI: http://127.0.0.1:8787.

Validation evidence: [118-test run](evidence/preservation/final-tests.log), [live-service checks](evidence/preservation/live-check.json), [independent parsing](evidence/preservation/independent-validation.json), and [synthetic before/after examples](evidence/preservation/examples/manifest.json). Regenerated fixture manifests use profile v3 and annotate now-retained content, including auxiliary metadata canaries. Common metadata/policy canaries can repeat across splits; primary synthetic body identities remain distinct.

Veil 0.4.1 uses [item-specific review criteria](REVIEW_CRITERIA.md). Source formulas that already lacked caches are informational; caches invalidated by redaction still require review/recalculation. Preservation behaviour is unchanged.
