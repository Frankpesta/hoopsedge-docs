"""ULIDs for runs and tickets (docs/12 section 4). They sort by creation time."""

import os
from collections.abc import Callable
from datetime import datetime

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LENGTH = 26


def new_ulid(now: datetime, randomness: Callable[[int], bytes] = os.urandom) -> str:
    number = (int(now.timestamp() * 1000) << 80) | int.from_bytes(randomness(10), "big")
    letters = []
    for _ in range(_LENGTH):
        letters.append(_ALPHABET[number & 31])
        number >>= 5
    return "".join(reversed(letters))
