"""One publication order everywhere (spec 2026-10-05 §6.1, P19)."""
import uuid
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from src.models import Publication
from src.services.tenure_scope import publication_order_by, publication_sort_key


def test_python_key_orders_year_then_numeric_pmid_then_doi_then_id():
    rows = [NS(year=None, pmid="1", doi=None, id=1), NS(year=2020, pmid="9", doi=None, id=2),
            NS(year=2020, pmid="10", doi=None, id=3), NS(year=2020, pmid=None, doi="10.1/b", id=4),
            NS(year=2020, pmid=None, doi="10.1/a", id=5), NS(year=2021, pmid="2", doi=None, id=6)]
    assert [r.id for r in sorted(rows, key=publication_sort_key)] == [6, 3, 2, 5, 4, 1]


def test_dict_records_without_doi_or_id_sort_too():
    recs = [{"year": 2020, "pmid": "100"}, {"year": 2020, "pmid": "200"}, {"year": 2021}]
    assert [r.get("pmid") for r in sorted(recs, key=publication_sort_key)] == [None, "200", "100"]


@pytest.mark.integration
async def test_sql_and_python_orders_agree(db_session):
    from tests import factories

    user = await factories.make_user(db_session)
    specs = [(2020, "9", None), (2020, "10", None), (2020, None, "10.1/Z-b"),
             (2020, None, "10.1/z_a"), (None, "5", None), (2019, "x12", "10.1/q"),
             (2020, None, None)]
    for year, pmid, doi in specs:
        db_session.add(Publication(id=uuid.uuid4(), user_id=user.id, year=year, pmid=pmid,
                                   doi=doi, title="t"))
    await db_session.flush()
    rows = (await db_session.execute(
        select(Publication).where(Publication.user_id == user.id)
        .order_by(*publication_order_by())
    )).scalars().all()
    assert [r.id for r in rows] == [r.id for r in sorted(rows, key=publication_sort_key)]
