"""Phone-number formats used across channels.

Two canonical forms exist in the data model (see README "Conventions"):

* **digits** — country code + national number, no ``+``. This is what WhatsApp
  sends as ``from`` and what ``patients.phone`` / ``transcripts.phone`` store.
* **E.164** — the same with a leading ``+``; used by ``conversations.phone``.

``normalize_phone`` converts the looser formats telephony providers hand us
(Exotel sends ``+91…`` but also ``0`` + 10 digits or bare 10-digit numbers) into
the digits form, assuming India (``91``) for national-format numbers. WhatsApp's
``from`` is already in digits form, and ``normalize_phone`` leaves digits-with-
country-code untouched, but the WhatsApp path deliberately does not call it (a
10-digit international number such as Norway's ``47`` + 8 would be mis-read as
an Indian national number).
"""

from __future__ import annotations

DEFAULT_COUNTRY_CODE = "91"
_NATIONAL_LEN = 10  # Indian national significant number length


def normalize_phone(raw: str | None, default_cc: str = DEFAULT_COUNTRY_CODE) -> str:
    """Return ``raw`` as digits-with-country-code (no ``+``), or ``""``.

    >>> normalize_phone("+91 98765-43210")
    '919876543210'
    >>> normalize_phone("09876543210")
    '919876543210'
    >>> normalize_phone("9876543210")
    '919876543210'
    """
    if not raw:
        return ""
    s = raw.strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if s.startswith("+"):
        return digits  # explicit international form
    if digits.startswith("00"):
        return digits[2:]  # international dialling prefix (00 + cc + number)
    if len(digits) == _NATIONAL_LEN + 1 and digits.startswith("0"):
        return default_cc + digits[1:]  # trunk prefix 0 + national number
    if len(digits) == _NATIONAL_LEN:
        return default_cc + digits  # bare national number
    return digits  # assume it already carries a country code


def to_e164(phone: str) -> str:
    """Return the ``+``-prefixed form of an already-canonical phone.

    Does not re-interpret the number (no country-code guessing), so it is safe
    for WhatsApp ``from`` values: ``"919876543210"`` → ``"+919876543210"``.
    """
    p = phone.strip()
    return p if p.startswith("+") else f"+{p}"
