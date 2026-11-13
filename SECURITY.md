# Security policy

## Supported release

Security fixes target the latest 1.x release. Keep dependencies and deployment images updated; a successful release scan reflects the advisory database at the time of the scan.

## Report a vulnerability

Use GitHub's **Report a vulnerability** option on this repository's Security tab. Do not place document text, credentials, cookie values, private keys or exploit details in a public issue. If private reporting is unavailable, open a public issue requesting a private contact channel without including sensitive details.

## Isolation boundary

Veil uses anonymous browser workspaces. Every document/batch operation derives ownership from a server-validated cookie, including batch uploads, nested ZIP members, original-text reveal, previews, reports, revisions and deletion. Random job IDs alone do not authorize access. Unknown, ownerless and foreign objects return the same not-found response. Old unowned data is never assigned by opening a link.

Cookies are opaque 256-bit random values, HttpOnly, Secure, host-only and SameSite=Strict over HTTPS. Only digests are stored. They expire after 24 hours idle and 7 days absolute. End workspace revokes the credential; existing downloads cannot be recalled. The same browser profile shares access across tabs. An attacker controlling that profile/device or stealing its credential can impersonate it. There are no named accounts or recovery codes.

Host administrators, filesystem access, backups and model-provider operators are separate trust boundaries. Server-side ownership does not encrypt retained files against administrators. No guarantee is made against a compromised host or provider.

## Deployment requirements

- Use HTTPS with a trusted certificate. The private-CA setup keeps its signing key outside the app's data mount. Never publish either private key, a cookie jar, `.env`, original documents or extracted text.
- Direct Compose deployment ignores forwarded protocol/identity headers. A different reverse-proxy deployment needs its own explicit trust configuration and review; arbitrary client headers must not become identity.
- Use the approved private network and perimeter controls. Anonymous workspaces are not internet-user verification. Monitor disk/CPU, apply gateway rate limits where appropriate, and restrict who can reach the service.
- Keep runtime directories private. The image runs without root or Linux capabilities, with a read-only root filesystem and bounded memory/CPU/tmpfs.
- Workspace creation has a per-peer hourly quota and global capacity. Multipart parsing allows one file and two small fields. Bodies, archive expansion, part/page counts, processing time, queue depth, inference concurrency, model calls and batch ZIP sizes are bounded.
- Never enable model network access without approving the endpoint and its handling of document text. Private-network HTTP model endpoints are supported; use HTTPS or an independently protected transport where required.

HTTP is supported for local development only on an actual loopback connection and hostname, using a separate cookie. LAN HTTP workspace/document operations are rejected. TLS protects browser traffic; it does not establish model detection quality.

## Content and retention

Originals, extracts, manual rules and retained detection values are sensitive. They are stored separately from downloadable artifacts. Reports/logs exclude detected source values. Source reveal is explicit and owner-authorized. Unsupported/executable Office content and unsafe XML/archive constructs are rejected; source matching and output preservation checks remain mandatory after manual corrections.

The configured model/OCR can miss PII, follow malicious document instructions incorrectly or return false positives. Strict JSON/source validation cannot prove all PII was detected. Review outputs before sharing. OCR text that was never extracted cannot be fixed by a text-only correction rule.

Normal server retention is 24 hours after a terminal job by default. Cleanup requires the worker supervisor. Stopped services, copied downloads and external backups follow their own operational policy. Ending browser access does not immediately delete files.

## Release controls

Release exports use an explicit source allowlist. CI rejects unapproved tracked paths, private-key/provider-token patterns, local home paths, nonempty secret examples and unpinned/hashless dependency locks. Independent secret scanning and dependency auditing run in CI. Checks never print matched secrets. If a credential was ever committed, ignoring/removing it is insufficient: revoke/rotate it and remove it from reachable history before republishing.
