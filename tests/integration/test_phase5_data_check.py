"""The Phase 5 read-only data checks run without error inside a read-only transaction
(spec 2026-10-15 §10 R5.11). The SQL is a verbatim copy of the Phase 5 plan's Deploy step 1
(a)-(d)."""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import Publication, User

pytestmark = pytest.mark.integration

CHECK_A = r"""
WITH c AS (SELECT '['||chr(13)||chr(10)||chr(11)||chr(12)||chr(133)||chr(8232)||chr(8233)||']' AS br)
SELECT count(*) FILTER (WHERE title ~ c.br) AS titles,
       count(*) FILTER (WHERE title ~ c.br AND excluded_at IS NULL) AS titles_in_use,
       count(*) FILTER (WHERE journal ~ c.br) AS journals,
       count(*) FILTER (WHERE journal ~ c.br AND excluded_at IS NULL) AS journals_in_use,
       count(*) FILTER (WHERE strpos(doi, '%') > 0) AS dois_with_percent,
       count(*) FILTER (WHERE strpos(doi, '%') > 0 AND excluded_at IS NULL) AS dois_with_percent_in_use
FROM publications, c
"""
CHECK_B = r"""
WITH c AS (SELECT '['||chr(13)||chr(10)||chr(11)||chr(12)||chr(133)||chr(8232)||chr(8233)||']' AS br,
                  '['||chr(13)||chr(11)||chr(12)||chr(133)||chr(8232)||chr(8233)||']' AS non_lf)
SELECT count(*) FILTER (WHERE research_summary ~ c.br) AS summaries_with_any_break,
       count(*) FILTER (WHERE research_summary ~ c.non_lf) AS summaries_with_non_lf_break
FROM researcher_profiles, c
"""
CHECK_C = r"""
WITH c AS (SELECT '['||chr(13)||chr(10)||chr(11)||chr(12)||chr(133)||chr(8232)||chr(8233)||']' AS br),
items AS (
  SELECT user_id, 'techniques' AS col, unnest(techniques) AS item FROM researcher_profiles
  UNION ALL SELECT user_id, 'experimental_models', unnest(experimental_models) FROM researcher_profiles
  UNION ALL SELECT user_id, 'disease_areas', unnest(disease_areas) FROM researcher_profiles
  UNION ALL SELECT user_id, 'key_targets', unnest(key_targets) FROM researcher_profiles
  UNION ALL SELECT user_id, 'keywords', unnest(keywords) FROM researcher_profiles)
SELECT col, count(*) AS items_with_break, count(DISTINCT user_id) AS profiles
FROM items, c WHERE item ~ c.br GROUP BY col ORDER BY col
"""
CHECK_D_VOLUME = r"""
SELECT count(*) FILTER (WHERE p.excluded_at IS NULL) AS in_use_rows,
       count(*) FILTER (WHERE p.excluded_reason IN ('staff','openalex_affiliation','repair_script')) AS not_theirs_rows,
       count(DISTINCT a.agent_id) AS agents_with_rows
FROM agents a JOIN publications p ON p.user_id = a.user_id
WHERE a.status = 'active'
"""
CHECK_D_JOBS = r"""
SELECT user_id, count(*) AS jobs_24h FROM jobs
WHERE type = 'generate_profile' AND enqueued_at > now() - interval '24 hours'
GROUP BY user_id HAVING count(*) >= 5
"""
CHECK_D_NON_DOI = r"""
SELECT count(*) AS pmidless_in_use_with_non_doi
FROM publications
WHERE pmid IS NULL AND excluded_at IS NULL AND doi IS NOT NULL
  AND doi !~* '^(doi:\s*|info:doi/|(https?://)?(www\.)?(dx\.)?doi\.org/)?10\.[0-9.]+/\S'
"""
STATEMENTS = (CHECK_A, CHECK_B, CHECK_C, CHECK_D_VOLUME, CHECK_D_JOBS, CHECK_D_NON_DOI)


async def _run_read_only(engine) -> list:
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        results = [(await conn.execute(text(sql))).mappings().all() for sql in STATEMENTS]
        await conn.rollback()
    return results


async def test_phase5_data_check_sql_is_read_only(engine):
    before = (await _run_read_only(engine))[0][0]
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        user = User(name="Data Check", access_status="allowed",
                    orcid=f"0000-0006-{uuid.uuid4().int % 10000:04d}-0005")
        s.add(user)
        await s.flush()
        s.add_all([
            Publication(user_id=user.id, pmid="31000901", title="Line\u2028break"),
            Publication(user_id=user.id, pmid=None, doi="10.1000/a%3Cb", title="Escaped DOI"),
        ])
        await s.commit()
    try:
        after = (await _run_read_only(engine))[0][0]
        assert after["titles"] - before["titles"] == 1
        assert after["dois_with_percent"] - before["dois_with_percent"] == 1
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            await s.commit()
