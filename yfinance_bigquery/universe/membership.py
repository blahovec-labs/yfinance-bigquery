"""Point-in-time S&P 500 membership reconstruction (survivorship-bias-free).

`reconstruct_membership` turns the *current* constituent list plus Wikipedia's
*dated changes* log into a set of membership spells — one row per
(symbol, date_added, date_removed) interval. Symbols that were removed before
the current snapshot are recovered as closed spells, which is the fix for
survivorship bias: a point-in-time universe drawn from current members alone
silently excludes everything that has since left the index.

`members_as_of_sql` builds the BigQuery query that answers "which symbols were
index members on date D".
"""

from __future__ import annotations

import pandas as pd

_SOURCE = "wikipedia"
_COLUMNS = ["symbol", "date_added", "date_removed", "source"]


def reconstruct_membership(
    *, current: pd.DataFrame, changes: pd.DataFrame
) -> pd.DataFrame:
    """Reconstruct membership spells from current members + dated changes.

    Args:
        current: DataFrame with at least ``symbol`` and ``date_added`` columns
            (the current constituent list, e.g. from ``fetch_constituents``).
        changes: DataFrame with ``date``, ``added_ticker``, ``removed_ticker``
            columns (e.g. from ``fetch_changes``).

    Returns:
        DataFrame ``[symbol, date_added, date_removed, source]``, one row per
        membership spell. Current members are OPEN spells (``date_removed`` is
        ``None``). A symbol that appears as a removal in ``changes`` but is not a
        current member is recovered as a CLOSED spell, with ``date_removed`` set
        to the change date and ``date_added`` to the matching prior addition date
        if one is present in ``changes`` (else ``None``).

    Known limitation (Phase 1): a symbol with MULTIPLE membership spells inside
    the window (removed, re-added, then removed again) is recorded as a single
    closed spell — full multi-spell reconstruction is a documented follow-up.
    Such cases are rare (a handful of tickers historically) and effectively
    absent from the ~2019+ window in practice.
    """
    current_symbols = {str(s) for s in current["symbol"]}

    # date a ticker was added, per the changes log (used to recover date_added
    # for symbols that have since been removed).
    add_dates: dict[str, object] = {}
    for row in changes.itertuples(index=False):
        added = getattr(row, "added_ticker", None)
        if not _is_na(added):
            add_dates.setdefault(str(added), getattr(row, "date", None))

    rows: list[dict[str, object]] = []

    # Open spells for every current member.
    for row in current.itertuples(index=False):
        rows.append(
            {
                "symbol": str(row.symbol),
                "date_added": getattr(row, "date_added", None),
                "date_removed": None,
                "source": _SOURCE,
            }
        )

    # Closed spells for symbols removed in the changes log that are no longer
    # current members (the survivorship recovery).
    recovered: set[str] = set()
    for row in changes.itertuples(index=False):
        removed = getattr(row, "removed_ticker", None)
        if _is_na(removed):
            continue
        sym = str(removed)
        if sym in current_symbols or sym in recovered:
            continue
        rows.append(
            {
                "symbol": sym,
                "date_added": add_dates.get(sym),
                "date_removed": getattr(row, "date", None),
                "source": _SOURCE,
            }
        )
        recovered.add(sym)

    return pd.DataFrame(rows, columns=_COLUMNS)


def members_as_of_sql(*, table: str) -> str:
    """Return BigQuery SQL selecting index members as of a bound ``@as_of`` DATE.

    The caller binds ``@as_of`` as a ``DATE`` ScalarQueryParameter.
    """
    return (
        f"SELECT DISTINCT symbol\n"
        f"FROM `{table}`\n"
        # NULL date_added = member since before the reconstruction window — must
        # NOT be excluded, or pre-window members vanish (survivorship bias).
        f"WHERE (date_added IS NULL OR date_added <= @as_of)\n"
        f"  AND (date_removed IS NULL OR date_removed > @as_of)"
    )


def _is_na(value: object) -> bool:
    """True if value is None or a pandas NA scalar."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
