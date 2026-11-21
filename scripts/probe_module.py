"""Pytest plugin: print which machina_deid module the test session loads."""
import machina_deid.production as _pr  # noqa: E402


def pytest_sessionstart(session):  # noqa: ANN001
    print(f"[probe] machina_deid.production = {_pr.__file__}")
