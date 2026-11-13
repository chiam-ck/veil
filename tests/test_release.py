"""Release boundaries protect secrets even if a maintainer stages the wrong file."""
from pathlib import Path
import shutil
import subprocess
import sys
import json
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from release_files import allowed,files
from release_check import check,environment_secrets,secret_name


def test_gitignore_blocks_private_material_and_keeps_source_examples(tmp_path):
    shutil.copyfile(ROOT/'.gitignore',tmp_path/'.gitignore')
    subprocess.run(['git','init','--quiet',str(tmp_path)],check=True)
    private=['.env','.env.production','dev.env','local.env.backup','.aws/credentials','.codex/state.json',
             '.veil/jobs/x/original/source.docx','.veil-ca/ca.key','private.key','tls/server.pem',
             'client.cookies','storage-state.json','local.sqlite3','docs/private.pdf','report.xlsx',
             'evidence/summary.json','checks/answer.json','inbox/message.json','feed.json','usage.json',
             'build/old.whl','app/dist/old.whl','dist/source.tar.gz','debug.log','screenshot.png','.netrc']
    for path in private:
        result=subprocess.run(['git','-C',str(tmp_path),'check-ignore','--quiet',path])
        assert result.returncode==0,path
    for path in ['.env.example','README.md','veil/api.py','veil/static/assets/veil-logo.png']:
        assert subprocess.run(['git','-C',str(tmp_path),'check-ignore','--quiet',path]).returncode==1,path


def test_export_allowlist_rejects_runtime_even_when_it_looks_like_source():
    for path in ['.env','evidence/code.py','.veil/file.py','scripts/local-secrets.py','app/dist/file.whl',
                 'veil/static/assets/source-document.png','../outside.py','/tmp/source.py']:
        assert not allowed(path),path
    assert allowed('veil/models/ch_PP-OCRv5_rec_mobile.onnx')
    assert allowed('.env.example') and allowed('.github/workflows/ci.yml')
    assert all(allowed(p.relative_to(ROOT)) for p in files(ROOT))


def test_secret_check_never_prints_a_matched_value(tmp_path,capsys):
    (tmp_path/'veil').mkdir();candidate=tmp_path/'veil'/'module.py'
    secret='sk-'+'private-credential-'+'a'*24
    env=tmp_path/'.env';env.write_text('VEIL_LLM_API_KEY='+secret+'\n')
    candidate.write_text('example='+repr(secret))
    results=check(tmp_path,[Path('veil/module.py')],environment_secrets(env))
    assert ('veil/module.py','runtime-secret-match') in results
    assert secret not in json.dumps(results)
    assert secret not in capsys.readouterr().out
    assert secret_name('VEIL_LLM_API_KEY') and not secret_name('VEIL_LLM_MAX_OUTPUT_TOKENS')


def test_release_check_rejects_symlink_to_local_secret(tmp_path):
    (tmp_path/'veil').mkdir();secret=tmp_path/'secret.txt';secret.write_text('private')
    (tmp_path/'veil'/'module.py').symlink_to(secret)
    assert check(tmp_path,[Path('veil/module.py')])==[('veil/module.py','symlink-or-external-path')]


def test_index_scan_detects_secret_even_when_worktree_was_cleaned(tmp_path):
    from release_check import check_index
    subprocess.run(['git','init','--quiet',str(tmp_path)],check=True)
    (tmp_path/'veil').mkdir();path=tmp_path/'veil'/'module.py'
    secret='sk-'+'stage-secret-'+'z'*32
    path.write_text('key='+repr(secret))
    subprocess.run(['git','-C',str(tmp_path),'add','veil/module.py'],check=True)
    path.write_text('# Visible file is clean, but the index still contains the key.')
    paths,findings=check_index(tmp_path,[secret.encode()])
    assert ('veil/module.py','runtime-secret-match') in findings
    assert secret not in json.dumps(findings)
