"""Bounded local analytics primitives for large TokenLens runs."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from statistics import quantiles
from tempfile import NamedTemporaryFile


class BoundedDistribution:
    """Deterministic bounded sample with stable percentile estimates."""

    def __init__(self, *, max_samples: int = 10_000) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self.max_samples = max_samples
        self.count = 0
        self._values: list[float] = []

    def add(self, value: float) -> None:
        self.count += 1
        if len(self._values) < self.max_samples:
            self._values.append(float(value))
            return
        # Deterministic systematic sampling: retain every k-th value as the
        # stream grows, avoiding random state and making reports reproducible.
        stride = max(2, self.count // self.max_samples)
        if self.count % stride == 0:
            self._values[(self.count // stride) % self.max_samples] = float(value)

    def extend(self, values) -> None:
        for value in values:
            self.add(value)

    def percentile(self, percent: float) -> float | None:
        if not self._values:
            return None
        ordered = sorted(self._values)
        if len(ordered) == 1:
            return ordered[0]
        index = (len(ordered) - 1) * percent / 100
        lower = int(index)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    def summary(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "sample_count": len(self._values),
            "p50": self.percentile(50),
            "p95": self.percentile(95),
            "p99": self.percentile(99),
        }


class SqliteSpill:
    """Run-scoped SQLite spill store for bounded local grouping."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._temporary = path is None
        self.path = Path(path) if path else Path(NamedTemporaryFile(prefix="tokenlens-", suffix=".sqlite", delete=False).name)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS values_store (group_key TEXT NOT NULL, value REAL NOT NULL)"
        )

    def add(self, group_key: str, value: float) -> None:
        self._connection.execute(
            "INSERT INTO values_store(group_key, value) VALUES (?, ?)",
            (group_key, float(value)),
        )

    def commit(self) -> None:
        self._connection.commit()

    def groups(self):
        self.commit()
        for group_key, in self._connection.execute(
            "SELECT DISTINCT group_key FROM values_store ORDER BY group_key"
        ):
            values = BoundedDistribution()
            for (value,) in self._connection.execute(
                "SELECT value FROM values_store WHERE group_key = ? ORDER BY rowid",
                (group_key,),
            ):
                values.add(value)
            yield group_key, values

    def close(self) -> None:
        self._connection.close()
        if self._temporary:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> "SqliteSpill":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
