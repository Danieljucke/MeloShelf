"""The line printed at startup must describe what the product really does."""
from meloshelf.__main__ import STARTUP_NOTICE


def test_the_startup_notice_does_not_claim_the_app_cannot_move_files():
    text = STARTUP_NOTICE.lower()
    assert 'read-only' not in text and 'disabled' not in text and 'phase 2' not in text
    assert 'confirm' in text and 'verified' in text
