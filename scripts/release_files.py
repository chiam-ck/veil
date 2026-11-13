"""Reviewed source allowlist: exports never recurse through runtime/evidence data."""
from pathlib import Path

ROOT_FILES = {
    '.env.example','.gitignore','.dockerignore','.gitattributes','Dockerfile',
    'README.md','DEVELOPER_GUIDE.md','PRESERVATION.md','REVIEW_CRITERIA.md',
    'THIRD_PARTY.md','SECURITY.md','CHANGELOG.md','RELEASING.md','LICENSE',
    'pyproject.toml','MANIFEST.in','requirements.in','requirements.lock','requirements-build.in',
    'requirements-build.lock','requirements-dev.in','requirements-dev.lock',
    'compose.yaml','compose.pilot.yaml','compose.secrets.yaml',
}
SCRIPTS = {
    'setup.sh','dev.sh','deploy.sh','setup_workspace_tls.py','test_live_llm.py',
    'release_files.py','release_check.py','prepare_release.py',
    'ui-llm.cjs','ui-theme.cjs','ui-provider-recovery.cjs','ui-revisions.cjs','ui-workspaces.cjs',
}
BRAND = {'veil-logo.png','veil-icon-16.png','veil-icon-32.png','veil-icon-48.png','veil-icon-180.png'}


def allowed(path):
    p=Path(path);parts=p.parts
    if p.is_absolute() or '..' in parts:return False
    if len(parts)==1:return p.name in ROOT_FILES
    if '__pycache__' in parts:return False
    if parts[0]=='veil':
        return (p.suffix=='.py' or str(p)=='veil/model-lock.json'
                or str(p)=='veil/models/ch_PP-OCRv5_rec_mobile.onnx'
                or (len(parts)==3 and parts[1]=='static' and p.suffix in {'.html','.css','.js','.md','.ico'})
                or (len(parts)==4 and parts[1:3]==('static','assets') and p.name in BRAND))
    if str(p) in {'docs/screenshots/batch-workspace.png','docs/screenshots/completed-results.png','docs/screenshots/manual-redaction.png'}:return True
    if parts[0]=='tests':return len(parts)==2 and p.suffix=='.py'
    if parts[0]=='scripts':return len(parts)==2 and p.name in SCRIPTS
    if parts[0]=='licenses':return len(parts)==2 and p.suffix in {'.txt','.md'}
    if parts[0]=='.github':return (len(parts)==3 and parts[1]=='workflows' and p.suffix in {'.yml','.yaml'}) or str(p)=='.github/dependabot.yml'
    return str(p) in {'app/README.md','.githooks/pre-commit'}


def files(root):
    root=Path(root)
    for name in sorted(ROOT_FILES):
        path=root/name
        if path.is_file():yield path
    # Only approved source subtrees are visited, never local .env/data/logs.
    for name in ['veil','tests','scripts','licenses','.github','.githooks','app','docs']:
        folder=root/name
        if not folder.is_dir():continue
        for path in sorted(folder.rglob('*')):
            if path.is_file() and allowed(path.relative_to(root)):yield path
