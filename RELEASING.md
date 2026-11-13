# Releasing Veil

A release must contain reviewed source and reproducible build instructions, never the deployed server's data/configuration. The project license is AGPL-3.0-only; upstream license/NOTICE files must accompany redistributed components.

## Prepare

1. Update `veil.__version__`, `pyproject.toml`, image tag, user-facing version labels and CHANGELOG.md together.
2. Regenerate runtime/build/development locks with `pip-compile --generate-hashes --allow-unsafe --no-emit-index-url --no-emit-trusted-host` against public PyPI. Review dependency changes; do not copy a private package-index URL into a lock.
3. Run tests, dependency auditing and source checks. Model coverage requires a separate representative evaluation; unit fixtures do not establish detection accuracy.
4. Check the actual staged/history contents. `.gitignore` prevents new accidental additions; it does not untrack files or erase previous commits. Revoke/rotate any credential ever committed before removing it from reachable history.

## Export a clean source tree

```bash
python scripts/release_check.py --secret-env .env
python scripts/prepare_release.py /tmp/veil-release-source --secret-env .env
cd /tmp/veil-release-source
```

The exporter copies only approved source files. It refuses to overwrite its destination or copy symlinks/external files. Private `.env` values are compared in memory and never emitted. It excludes documents, runtime data, certificates/keys, cookie jars, screenshots, logs, generated builds and local operational/agent state. Keep raw audit/browser evidence local; publish only reviewed summaries.

Build and publish from the clean source tree, not a deployed directory with old build metadata. Initialize/stage Git there if it is a new repository. Enable the supplied index-scanning hook with `git config core.hooksPath .githooks` (do not override an existing custom hook setup without merging it), then run:

```bash
python scripts/release_check.py --tracked
gitleaks git --redact=100 --no-banner --log-opts=--all
pytest -q
pip-audit -r requirements.lock --no-deps --disable-pip
pip-audit -r requirements-dev.lock --no-deps --disable-pip
python -m build --no-isolation
docker build -t veil:1.0.0 .
```

Install the hash-pinned development lock first and the project with `--no-deps --no-build-isolation`. Inspect the source archive and wheel contents with the same allowlist and secret checks. Release artifacts include the source archive, wheel, checksums and a dependency inventory. Do not upload cookies, private CA keys, `.env`, database snapshots or raw document evidence as release assets.

## GitHub

Use a reviewed initial commit for a new repository. Run CI on the pushed source before publishing the matching annotated version tag and GitHub release. Enable secret scanning, push protection and private vulnerability reporting. CI permissions are read-only; publishing is an explicit maintainer operation, never a pull-request workflow with secrets.

The shipped UI links to the matching version's public source. Modified network deployments must preserve an appropriate corresponding-source offer. Repository visibility does not grant a commercial dependency license.

## Deploy or roll back

Generate/trust the deployment certificate, set the private provider configuration and use `scripts/deploy.sh`. It validates configuration without printing secrets, builds the image, waits for active processing before restarting Veil and verifies HTTPS health. It does not erase runtime storage or assign legacy ownerless jobs to a visitor. Back up the private data volume separately according to your policy before upgrades.

Never roll back an ownership-aware database to an old unauthenticated app. A rollback must retain document authorization and TLS. Existing certificate identity and workspace cookies must not be regenerated as a side effect of a routine image update.
