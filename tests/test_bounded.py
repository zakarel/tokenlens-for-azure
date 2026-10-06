from tokenlens.bounded import BoundedDistribution, SqliteSpill


def test_bounded_distribution_preserves_small_dataset_percentiles():
    values = BoundedDistribution(max_samples=100)
    values.extend(range(1, 101))
    assert values.count == 100
    assert values.percentile(50) == 50.5
    assert values.percentile(95) == 95.05


def test_bounded_distribution_caps_sample_for_large_stream():
    values = BoundedDistribution(max_samples=10)
    values.extend(range(100_000))
    assert values.count == 100_000
    assert values.summary()["sample_count"] <= 10


def test_sqlite_spill_groups_deterministically_and_cleans_up(tmp_path):
    path = tmp_path / "spill.sqlite"
    with SqliteSpill(path) as spill:
        for value in [3, 1, 2]:
            spill.add("a", value)
        spill.add("b", 10)
        groups = dict(spill.groups())
        assert list(groups) == ["a", "b"]
        assert groups["a"].percentile(50) == 2
    assert path.exists()
