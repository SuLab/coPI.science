import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_LARGE = {
    "response_text", "system_prompt", "messages_json", "messages", "request", "raw_request", "prompt",
}


def test_sampling_query_selects_no_large_text_columns():
    import scripts.dev.opus55_replay as r
    stmt = r.sampling_statement([uuid.uuid4()], "claude-opus-5")
    names = {c.key for c in stmt.selected_columns}
    assert names & _LARGE == set()
    assert {"id", "phase", "created_at", "has_sidecar"} <= names
