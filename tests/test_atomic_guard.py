"""atomic_replace must refuse to replace a slice with far fewer rows than it holds.

Atomic writes stop a *failed* load from wiping data. They don't stop a load that
*succeeds* with a short frame: an upstream outage that returns half a week (or an
empty season) would still replace good rows with fewer. The guard counts the slice
before swapping and refuses when the new frame is under half of it.

Defaults: refuse when incoming < 50% of existing AND existing >= 100 rows. Small
slices are exempt (normal corrections). BIGQUERY_ALLOW_SHRINK=1 overrides, for a
source that really did shrink. A refusal deletes nothing and drops staging.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest
from google.cloud import bigquery

from yfinance_bigquery._atomic import ShrinkGuardError, atomic_replace

TARGET = "p.d.t"
SCHEMA = [bigquery.SchemaField("k", "INT64")]
WHERE = "k BETWEEN 0 AND 1000000"


def _client(existing: int) -> MagicMock:
    c = MagicMock(spec=bigquery.Client)

    def query_and_wait(sql, **_kwargs):
        if "COUNT(*)" in sql:
            return [(existing,)]
        return MagicMock()

    c.query_and_wait.side_effect = query_and_wait
    return c


def _df(n: int) -> pd.DataFrame:
    return pd.DataFrame({"k": range(n)})


def _deletes(c: MagicMock) -> list[str]:
    return [call.args[0] for call in c.query_and_wait.call_args_list if "DELETE" in call.args[0]]


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("BIGQUERY_ALLOW_SHRINK", raising=False)


def test_refuses_to_replace_a_slice_with_under_half_its_rows():
    c = _client(existing=1000)
    with pytest.raises(ShrinkGuardError) as exc:
        atomic_replace(c, TARGET, _df(499), SCHEMA, WHERE)
    assert _deletes(c) == []
    assert exc.value.existing == 1000 and exc.value.incoming == 499
    assert "BIGQUERY_ALLOW_SHRINK" in str(exc.value)


def test_refusal_still_drops_the_staging_table():
    c = _client(existing=1000)
    with pytest.raises(ShrinkGuardError):
        atomic_replace(c, TARGET, _df(10), SCHEMA, WHERE)
    staging = str(c.load_table_from_dataframe.call_args.args[1])
    assert staging in [str(call.args[0]) for call in c.delete_table.call_args_list]


def test_refuses_an_empty_frame_over_a_populated_slice():
    c = _client(existing=500)
    with pytest.raises(ShrinkGuardError):
        atomic_replace(c, TARGET, _df(0), SCHEMA, WHERE)
    assert _deletes(c) == []


def test_allows_exactly_half():
    c = _client(existing=1000)
    assert atomic_replace(c, TARGET, _df(500), SCHEMA, WHERE) == 500
    assert len(_deletes(c)) == 1


def test_small_slices_are_exempt():
    c = _client(existing=99)
    assert atomic_replace(c, TARGET, _df(1), SCHEMA, WHERE) == 1
    assert len(_deletes(c)) == 1


def test_growth_and_first_loads_are_allowed():
    c = _client(existing=0)
    assert atomic_replace(c, TARGET, _df(5000), SCHEMA, WHERE) == 5000


def test_env_override_allows_a_real_shrink(monkeypatch):
    monkeypatch.setenv("BIGQUERY_ALLOW_SHRINK", "1")
    c = _client(existing=1000)
    assert atomic_replace(c, TARGET, _df(10), SCHEMA, WHERE) == 10
    assert len(_deletes(c)) == 1


def test_count_uses_the_same_where_and_parameters_as_the_delete():
    c = _client(existing=0)
    params = [bigquery.ScalarQueryParameter("x", "INT64", 7)]
    atomic_replace(c, TARGET, _df(1), SCHEMA, "k = @x", params)
    count = next(call for call in c.query_and_wait.call_args_list if "COUNT(*)" in call.args[0])
    assert f"FROM `{TARGET}` WHERE k = @x" in count.args[0]
    assert count.kwargs["job_config"].query_parameters == params
