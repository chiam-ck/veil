# What “review required” means

Veil 0.4.1 separates **review tasks** from **processing information**. An informational notice does not change a successful job to `review_required`. The report identifies each review task's reason, affected location and suggested action. The UI shows these tasks prominently and keeps information in a collapsed section.

`completed` still means the supported pipeline completed, not that somebody reviewed the file or that no personal data remains. The original review update described here predates the optional LLM detector.

## Current criteria

| Observation | Outcome | Reason |
|---|---|---|
| An exactly uniform image, including Word's default white thumbnail | Information | Pixel inspection confirms there is no visual text. OCR still runs. |
| Empty standard Office bibliography, its recognized schema metadata and standard relationships | Information | No arbitrary custom data is present in these recognized structures; text/attribute PII inspection still runs. Nonempty or unknown custom XML requires review. |
| A supported formula already lacked a cached result in the original | Information | Veil preserved the formula and did not introduce the missing cache. It does not evaluate formulas. Open in a trusted spreadsheet application if results are needed. |
| Supported OCR text, every recognized line at confidence ≥0.8, height ≥12 input pixels and rotation ≤10° | Information | No specific OCR quality flag was observed. This is not a guarantee that OCR found every line or that detection found every PII entity. |
| Nonblank image with no recognized text | Review | OCR coverage is uncertain. A logo is not guessed to be harmless. |
| OCR lines below these confidence/size/rotation thresholds | Review | Inspect the indicated image or PDF page for missed/incomplete redactions. |
| Unsupported script observed | Review | Detector language coverage is incomplete. |
| Saved PPTX printer settings | Information | Removed from redacted copies as selected for this deployment; slides and layout are preserved. |
| Retained Word/Excel printer settings, unknown custom XML or extended comment schemas | Review | These remain incompletely inspected even though preserved. |
| Redaction invalidated formula caches | Review | Recalculation and result checking are needed for the indicated cells. |
| Conflicting identity replacements across revision views | Review | Compare the affected original/final revision views. |
| Annotation icons removed from output, or plain stroked straight PDF borders/table rules | Information | They do not automatically trigger the previous outlined-text warning. More complex retained vector drawings are passed through OCR. Removed annotation icons do not trigger this check. |
| Unsupported embedded/executable content, unsupported formulas, known PII residuals | Blocked | The update does not relax these guards or publish partial output. |

These thresholds are versioned heuristics, not calibrated probabilities or an organisation's policy. High OCR recognition confidence does not measure missed text regions, and says nothing about PII detector recall. English, Simplified/Traditional Chinese and mixed content use the same criteria. A native Chinese document does not require review simply because it contains Chinese.

The blank-image rule requires **exactly constant RGB channels across all pixels**, not a low-contrast threshold that could misclassify faint writing as blank. A nonblank thumbnail that OCR cannot read still requires review. PowerPoint printer settings are removed rather than treated as inspected. The report records this removal; it does not require manual review of the removed settings.

## Reports, API and CLI

- `issues`: structured, value-free findings with `code`, `severity` (`info` or `review`), `message`, `location`, `location_label` and `action`.
- `warnings`: review messages retained for older integrations. Informational messages are in `notices` instead.
- `review_summary`: required flag and counts for review/information items. A blocked/failed status takes precedence even if some review findings were collected first.
- `review_policy`: `veil-evidence-review-v1`, with the actual OCR thresholds. The underlying preservation profile remains `veil-preserve-content-v3`.
- Job/batch status includes a summary and the first three review reasons. The UI lists individual locations/actions; long lists are capped at 100 displayed items and remain complete in the JSON report.
- CLI exit codes stay the same: 0 completed, 2 review_required/blocked, 1 failure, 64 usage error. A completed job may contain informational notices.

Reports never include the OCR text, original entity values or formula expressions. Location labels use page/cell numbers and opaque Office part IDs, not potentially sensitive embedded filenames.

## Existing results

Historical results retain their original status, report and processing version. Reloading the UI does not retroactively certify them. The interface labels reports without the new review criteria as older results; **resubmit the original document** to apply the new criteria. Originals are never overwritten and the change does not trigger automatic reprocessing.

## Validation

See `evidence/review-policy/` for test results, fixture classifications and browser checks. These checks measure status behaviour and protect existing redaction/preservation; fewer review labels are not evidence of higher PII recall. Earlier English/Chinese accuracy benchmarks remain historical measurements of their named processing versions. No Microsoft Office compatibility certification or exhaustive manual inspection is implied.
