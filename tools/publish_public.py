"""Explicit, audited GitHub publication. No local git installation or device access needed."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare_github_release import audit, source_files
from windows_app.update_contract import REPOSITORY, CURRENT_VERSION, WINDOWS_NAME, FIRMWARE_NAME, MANIFEST_NAME, SIGNATURE_NAME, verify_artifact
from windows_app.updates import verify_manifest


def gh(*args, data=None, optional=False):
    command = ['gh', *args]
    if data is not None:
        command += ['--input', '-']
    process = subprocess.run(command, input=None if data is None else json.dumps(data).encode('utf-8'),
        capture_output=True, timeout=180, cwd=ROOT)
    if process.returncode:
        if optional:
            return None
        raise RuntimeError('GitHub operation failed: '+str(args[:3]))
    return json.loads(process.stdout) if process.stdout.strip().startswith((b'{', b'[')) else process.stdout.decode('utf-8').strip()


def api(path, method='GET', data=None, optional=False):
    return gh('api', path, '--method', method, data=data, optional=optional)


def publish_source(stage):
    expected = source_files()
    staged = {p.relative_to(stage).as_posix(): p.read_bytes() for p in stage.rglob('*') if p.is_file()}
    if staged != expected:
        raise RuntimeError('Staged sources differ from the current audited allowlist; restage explicitly.')
    audit(staged)
    repo = api('repos/'+REPOSITORY)
    if repo['private'] or repo['default_branch'] != 'main':
        raise RuntimeError('Expected the explicitly approved public main-branch repository.')
    ref = api('repos/'+REPOSITORY+'/git/ref/heads/main', optional=True)
    if ref is None:
        if api('repos/'+REPOSITORY+'/contents', optional=True) is not None or repo['size'] != 0:
            raise RuntimeError('Repository is not the approved empty repository; refusing initialization.')
        initial = api('repos/'+REPOSITORY+'/contents/README.md', 'PUT', dict(
            message='Initialize sanitized PDKS public sources', branch='main',
            content=base64.b64encode(staged['README.md']).decode('ascii')))
        parent = initial['commit']['sha']
    else:
        # Never overwrite an unexpectedly modified remote tree without an explicit new review.
        raise RuntimeError('Repository already has commits; review remote changes before a subsequent source publication.')
    tree = []
    for name, content in sorted(staged.items()):
        entry = dict(path=name, mode='100644', type='blob')
        try:
            entry['content'] = content.decode('utf-8')
        except UnicodeError:
            blob = api('repos/'+REPOSITORY+'/git/blobs', 'POST',
                dict(content=base64.b64encode(content).decode('ascii'), encoding='base64'))
            entry['sha'] = blob['sha']
        tree.append(entry)
    created = api('repos/'+REPOSITORY+'/git/trees', 'POST', dict(tree=tree))
    commit = api('repos/'+REPOSITORY+'/git/commits', 'POST', dict(
        message='PDKS v'+CURRENT_VERSION+': signed Windows and LAN firmware updates', tree=created['sha'], parents=[parent]))
    # Ref update is non-force; concurrent remote changes cause a safe failure.
    api('repos/'+REPOSITORY+'/git/refs/heads/main', 'PATCH', dict(sha=commit['sha'], force=False))
    actual = api('repos/'+REPOSITORY+'/git/trees/'+created['sha']+'?recursive=1')
    wanted = set(staged)
    found = {entry['path'] for entry in actual['tree'] if entry['type']=='blob'}
    if wanted != found or actual.get('truncated'):
        raise RuntimeError('Published source inventory does not match the audited allowlist.')
    for entry in actual['tree']:
        if entry['type'] == 'blob':
            content = staged[entry['path']]
            digest = hashlib.sha1(b'blob '+str(len(content)).encode('ascii')+b'\0'+content).hexdigest()
            if digest != entry['sha']:
                raise RuntimeError('Published source bytes differ from the audited staging tree.')
    print('SOURCE COMMIT:', commit['sha'], 'files:', len(wanted))
    return commit['sha']


def publish_release(assets, commit):
    release = verify_manifest((assets/MANIFEST_NAME).read_bytes(), (assets/SIGNATURE_NAME).read_bytes())
    manifest = release.manifest
    files = (WINDOWS_NAME, FIRMWARE_NAME, MANIFEST_NAME, SIGNATURE_NAME)
    if {p.name for p in assets.iterdir()} != set(files):
        raise RuntimeError('Only the four audited assets may be published.')
    for kind in ('windows', 'firmware'):
        verify_artifact(assets/manifest[kind]['name'], manifest[kind])
    audit({name: (assets/name).read_bytes() for name in files})
    tag = 'v'+CURRENT_VERSION
    if api('repos/'+REPOSITORY+'/releases/tags/'+tag, optional=True) is not None:
        raise RuntimeError('Release already exists; do not overwrite published artifacts.')
    gh('release', 'create', tag, *(str(assets/name) for name in files), '--repo', REPOSITORY,
       '--target', commit, '--draft', '--title', 'PDKS Yönetim '+tag,
       '--notes', manifest['notes']+'\n\nİlk OTA kurulumu: bir kez özel USB gerekir. '
       'Public firmware boş cihazın ilk kurulum imajı değildir. '
       'Gerçek cihaz OTA kabul testi henüz yapılmadı. '
       'EXE uygulama içi imzayla doğrulanır; Authenticode imzası değildir.')
    gh('release', 'edit', tag, '--repo', REPOSITORY, '--draft=false', '--latest')
    print('RELEASE:', 'https://github.com/'+REPOSITORY+'/releases/tag/'+tag)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--publish', action='store_true', required=True)
    args = parser.parse_args()
    if api('user')['login'] != REPOSITORY.split('/')[0]:
        raise RuntimeError('Authenticated publisher is not the approved account.')
    commit = publish_source(args.source)
    publish_release(args.assets, commit)
