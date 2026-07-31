import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to path atomically (mkstemp + write + os.replace).

    Used by admin write endpoints that rewrite config files in place from
    inside the running container -- os.replace is atomic on POSIX, so a
    crash or concurrent read never observes a partially-written file.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        # mkstemp creates the temp file at mode 0600 -- if we're replacing
        # an existing file, match its mode instead of silently narrowing
        # permissions on every admin write (this matters in particular when
        # the gateway container runs as root against a host bind mount: the
        # file's owner becomes root regardless, but there's no reason to
        # also strip host group/other read access that was there before).
        if path.exists():
            os.chmod(tmp_name, path.stat().st_mode)
        else:
            os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise
