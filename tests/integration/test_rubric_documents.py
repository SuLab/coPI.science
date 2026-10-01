import hashlib
import inspect

import pytest
from sqlalchemy import delete, select

from src.models import RubricDocument
from src.services import blackbird_rubric as br
from src.services import rubric_revisions
from src.services.rubric_documents import record_loaded_rubric

pytestmark = pytest.mark.integration


async def test_records_the_loaded_rubric_once(db_session):
    try:
        await record_loaded_rubric(db_session)
        await record_loaded_rubric(db_session)
        rows = (await db_session.execute(select(RubricDocument))).scalars().all()
        mine = [r for r in rows if r.content_hash == br.RUBRIC_CONTENT_HASH]
        assert len(mine) == 1
        raw = br.loaded_rubric_bytes()
        assert mine[0].sha256 == hashlib.sha256(raw).hexdigest()
        assert mine[0].toml == raw.decode("utf-8")
        assert mine[0].version == br.RUBRIC_VERSION
    finally:
        await db_session.execute(
            delete(RubricDocument).where(RubricDocument.content_hash == br.RUBRIC_CONTENT_HASH)
        )
        await db_session.commit()


def test_parsed_rubric_is_unchanged():
    assert br.parse_rubric_bytes(br.loaded_rubric_bytes(), str(br.RUBRIC_PATH)) == br.load_rubric()


def test_resolution_never_reads_the_table():
    assert "RubricDocument" not in inspect.getsource(rubric_revisions)
    assert "rubric_documents" not in inspect.getsource(rubric_revisions)
