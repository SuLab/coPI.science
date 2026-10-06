import hashlib
import uuid

from src.services import advisory_locks as al
from src.services.assessment_chat import _SPEND_LOCK_KEY


def test_fixed_key_is_signed_big_endian_sha256_prefix():
    expected = int.from_bytes(hashlib.sha256(b"engine").digest()[:8], "big", signed=True)
    assert al.fixed_key("engine") == expected == al.ENGINE_LOCK_KEY


def test_fixed_keys_are_distinct_from_each_other_and_the_spend_lock():
    keys = [al.ENGINE_LOCK_KEY, al.WORKER_LOCK_KEY, al.ADMIN_INVARIANT_LOCK_KEY, al.HUB_ROSTER_LOCK_KEY, _SPEND_LOCK_KEY]
    assert len(set(keys)) == len(keys)


def test_entity_key_sql_casts_the_bind():
    assert al.entity_key_sql("corpus") == "hashtextextended('corpus:' || CAST(:id AS text), 0)"


async def test_lock_agent_persona_uses_the_agent_namespace():
    seen = []

    class _Db:
        async def execute(self, statement, params):
            seen.append((str(statement), params))

    uid = uuid.uuid4()
    await al.lock_agent_persona(_Db(), uid)
    assert seen == [(
        "SELECT pg_advisory_xact_lock(hashtextextended('agent:' || CAST(:id AS text), 0))",
        {"id": str(uid)},
    )]
