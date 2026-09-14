from contextlib import contextmanager
from pathlib import Path
import shutil
import uuid


@contextmanager
def temporary_directory():
    """Use inherited workspace ACLs; this host rejects tempfile's private ACL."""
    base = Path(__file__).resolve().parents[1] / ".test-runs"
    base.mkdir(exist_ok=True)
    target = base / uuid.uuid4().hex
    target.mkdir()
    try:
        yield str(target)
    finally:
        resolved = target.resolve()
        if resolved.parent != base.resolve():
            raise ValueError("Refusing cleanup outside isolated test directory")
        shutil.rmtree(resolved)
