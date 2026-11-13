# Third-party components

Veil's own source is AGPL-3.0-only; see LICENSE. Third-party dependencies keep their upstream terms. Exact dependency versions and wheel/source hashes are recorded in `requirements.lock`; build/development tools have separate locks. `veil/model-lock.json` records checksums for the local OCR/OpenCC assets checked at startup.

| Component | Upstream license / notice |
|---|---|
| PyMuPDF / MuPDF | AGPL-3.0 or an Artifex commercial license. This release uses the AGPL path and supplies source/build instructions; no commercial license is included. [Upstream terms](https://pymupdf.io/licensing). |
| RapidOCR and bundled PaddleOCR models | Apache-2.0; see `licenses/RapidOCR-APACHE-2.0.txt` and `licenses/PaddleOCR-APACHE-2.0.txt`. |
| Bundled PP-OCRv5 mobile recognition ONNX asset | PaddleOCR/RapidAI model asset, Apache-2.0. SHA256 and filename are in `veil/model-lock.json`; [upstream model inventory](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml). |
| OpenCC / opencc-python-reimplemented | Apache-2.0. Preserve the package's NOTICE and `licenses/OpenCC-APACHE-2.0.txt`. |
| FastAPI, Starlette, ONNX Runtime, python-docx, python-pptx, openpyxl and most Python utilities | Permissive upstream licenses distributed with their packages. |
| OpenCV | Apache-2.0 and bundled third-party notices. |
| Pillow, lxml/libxml2, NumPy/BLAS, image codecs, fonts and Debian runtime libraries | Their respective distribution licenses/notices. Preserve the installed packages' notices when redistributing containers. |
| Optional Playwright / Chrome and release-scanning tools | Development tools installed separately; browser binaries are not shipped in the source package or runtime image. Their upstream terms apply. |

No local NER engine or model is installed. Historical local benchmark artifacts and former dependency inventories are not part of this release. A dependency audit checks known advisories, not license compatibility or detection accuracy.

For network deployments, keep the source link visible to users and provide the corresponding version's source and build instructions. Modified deployments must meet their applicable AGPL obligations; this file does not provide a legal certification or a commercial dependency license.
