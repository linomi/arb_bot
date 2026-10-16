import pytest


@pytest.fixture(autouse=True)
def _isolated_contract_size_cache(tmp_path, monkeypatch):
    """Never let tests read or write the real data/xt_contract_sizes.json."""
    from backend.exchange import contract_sizes
    contract_sizes.reset_for_tests(tmp_path / "xt_contract_sizes.json")
    yield
    contract_sizes.reset_for_tests()
