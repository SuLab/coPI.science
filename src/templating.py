"""One Jinja environment for every router, with the compiled-CSS cache key as a global."""
import hashlib
import logging
from pathlib import Path

from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = REPO_ROOT / "static" / "css" / "app.min.css"


_cache: dict[Path, tuple[tuple[int, int] | None, str]] = {}


def css_version(path: Path = CSS_PATH) -> str:
    """First 12 hex chars of the stylesheet's sha256, or "missing" (logged) when absent.

    Cached on (mtime_ns, size), so a render costs one stat() in the steady state while a
    rebuilt stylesheet (dev `--reload`, or a build after start-up) still gets a new key.
    """
    try:
        st = path.stat()
        key: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        key = None
    cached = _cache.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    if key is None:
        logger.error("compiled stylesheet %s missing; run scripts/build-css.sh", path)
        value = "missing"
    else:
        value = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    _cache[path] = (key, value)
    return value


templates = Jinja2Templates(directory="templates")
# Registered as a callable so each render re-reads the file: a stylesheet rebuilt under
# `uvicorn --reload` (or built after the process started) gets a fresh cache key.
templates.env.globals["css_version"] = css_version
