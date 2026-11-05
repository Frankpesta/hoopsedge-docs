"""D11: the SportyBet code must contain no login, wallet or bet-placement capability."""

import re
from pathlib import Path

SERVICES = Path(__file__).resolve().parent.parent
GUARDED = [
    *sorted((SERVICES / "hoopsedge" / "sportybet").rglob("*.*")),
    SERVICES.parent / "scripts" / "sportybet_discovery.py",
]
FORBIDDEN = re.compile(r"login|password|wallet|deposit|placeBet|/orders/(?!share)", re.IGNORECASE)


def test_guard_pattern_catches_forbidden_calls() -> None:
    for text in ("/api/ng/orders/order", "placeBet(", "user_login", "Wallet", "deposit"):
        assert FORBIDDEN.search(text), text
    assert FORBIDDEN.search("/api/ng/orders/share/ABC123") is None


def test_sportybet_code_has_no_betting_capability() -> None:
    assert GUARDED[-1].exists()
    hits = [
        f"{path.name}:{number}: {line.strip()}"
        for path in GUARDED
        if path.suffix in {".py", ".yaml"}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN.search(line)
    ]
    assert hits == []
