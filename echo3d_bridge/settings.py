"""Per-user desktop settings, independent of the selected model directory."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _app_data() -> Path:
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home())))
    return base / 'Tripo' / 'Echo3DBridge'


APP_DATA = _app_data()
DEFAULT_INBOX = APP_DATA / 'received'
SETTINGS_FILE = APP_DATA / 'settings.json'


def load_inbox(settings_file: Path = SETTINGS_FILE) -> Path:
    if not settings_file.exists():
        return DEFAULT_INBOX.resolve()
    data = json.loads(settings_file.read_text(encoding='utf-8'))
    value = data.get('cacheDirectory')
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ValueError('Invalid cache folder setting. Select a folder again.')
    return Path(value).resolve()


def validate_inbox(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError('Select an absolute path.')
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    # Test actual create/write access, including network drives and ACLs.
    with tempfile.TemporaryFile(dir=path) as stream:
        stream.write(b'echo3d-cache-check')
        stream.flush()
        os.fsync(stream.fileno())
    return path


def clear_cached_transfers(root: Path, protected=()):
    """Delete transfer folders inside the cache directory.

    The directory itself, loose files, symlinks, and any folder that contains
    a transfer still in progress are kept.
    """
    try:
        root = Path(root).resolve()
    except OSError:
        return 0
    keep = set()
    for item in protected:
        try:
            path = Path(item).resolve()
        except OSError:
            continue
        current = path if path.is_dir() else path.parent
        while current != root and root in current.parents:
            if current.parent == root:
                keep.add(current)
                break
            current = current.parent
    if not root.is_dir():
        return 0
    removed = 0
    for entry in list(root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if resolved == root or root not in resolved.parents or resolved in keep:
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
        except OSError:
            continue
    return removed


def save_inbox(path: Path, settings_file: Path = SETTINGS_FILE):
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=settings_file.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'cacheDirectory': str(path)}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(settings_file)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
