"""One Jinja environment for every router, with the compiled-CSS cache key as a global."""
import hashlib
import logging
from pathlib import Path

from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = REPO_ROOT / "static" / "css" / "app.min.css"


def css_version(path: Path = CSS_PATH) -> str:
    """First 12 hex chars of the stylesheet's sha256, or "missing" (logged) when absent."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        logger.error("compiled stylesheet %s missing; run scripts/build-css.sh", path)
        return "missing"


templates = Jinja2Templates(directory="templates")
# Registered as a callable so each render re-reads the file: a stylesheet rebuilt under
# `uvicorn --reload` (or built after the process started) gets a fresh cache key.
templates.env.globals["css_version"] = css_version
