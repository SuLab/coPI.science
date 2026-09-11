"""Company classification: pharma/biotech vs device/dx vs CRO/vendor vs other.

Rule-based on purpose (adversarial A2): OpenAlex says only ``type: company``;
a reagent vendor and a pharma are both companies. The vendor list is curated
YAML shipped alongside this module (not under data/, which is gitignored) so
it ships in the image without a code change to grow it."""
import re
from functools import lru_cache
from pathlib import Path

import yaml

_VENDOR_YAML = Path(__file__).with_name("vendor_blocklist.yaml")
_PHARMA = re.compile(r"\b(pharma\w*|therapeutics|biotherapeutics|biosciences|biopharma\w*|oncology|vaccines?|genomics|biologics|"
                     r"pfizer|merck|novartis|roche|genentech|astrazeneca|glaxosmithkline|gsk|sanofi|bayer|abbvie|amgen|gilead|lilly|"
                     r"bristol|takeda|regeneron|moderna|biontech|vertex|biogen|boehringer|paratek|xtalpi|collaborations pharmaceuticals)\b", re.I)
_DEVICE = re.compile(r"\b(medtronic|boston scientific|abbott|stryker|siemens healthineers|philips|ge healthcare|becton|dickinson|"
                     r"illumina|diagnostics?|medical devices?|imaging|dexcom|intuitive surgical|edwards lifesciences)\b", re.I)


@lru_cache(maxsize=1)
def _vendor_terms() -> list[str]:
    if not _VENDOR_YAML.exists():
        return []
    data = yaml.safe_load(_VENDOR_YAML.read_text()) or {}
    return [t.lower() for t in data.get("vendors", [])]


def classify_company(name: str, openalex_type: str | None) -> str:
    n = (name or "").lower()
    if any(v in n for v in _vendor_terms()):
        return "cro_vendor"
    if _DEVICE.search(n):
        return "device_dx"
    if _PHARMA.search(n):
        return "pharma_biotech"
    return "other" if openalex_type == "company" or n else "unknown"
