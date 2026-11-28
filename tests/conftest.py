import pytest


@pytest.fixture(autouse=True)
def _no_hmi_replay_by_default(monkeypatch):
    """The packaged RemoteHMI replay is on by default in tftpush; the fake TFT in these tests
    doesn't speak createHMISession, so tests turn it on themselves when they need it."""
    monkeypatch.setenv("TFTPUSH_HMI_CAPTURE", "")
