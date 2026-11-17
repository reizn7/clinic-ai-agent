"""Phone-number formats used across channels.

Two canonical forms exist in the data model (see README "Conventions"):

* **digits** — country code + national number, no ``+``. This is what WhatsApp
  sends as ``from`` and what ``patients.phone`` / ``transcripts.phone`` store.
* **E.164** — the same with a leading ``+``; used by ``conversations.phone``.

``normalize_phone`` converts the looser formats people type into the browser call
page (``+91 98765 43210``, ``0`` + 10 digits, a bare 10-digit number…) into the
digits form, assuming India (``91``) for national-format numbers, and
``is_valid_phone`` decides whether what they typed is a usable number. WhatsApp's
``from`` is already in digits form, and ``normalize_phone`` leaves digits-with-
country-code untouched, but the WhatsApp path deliberately does not call it (a
10-digit international number such as Norway's ``47`` + 8 would be mis-read as
an Indian national number).
"""

from __future__ import annotations

DEFAULT_COUNTRY_CODE = "91"
_NATIONAL_LEN = 10  # Indian national significant number length
_E164_MIN, _E164_MAX = 8, 15  # digits incl. country code (E.164 allows up to 15)
_TYPED_CHARS = frozenset("0123456789+ -().")


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


def is_valid_phone(raw: str | None, default_cc: str = DEFAULT_COUNTRY_CODE) -> bool:
    """Is ``raw`` (as a person typed it) a plausible phone number?

    Only phone punctuation is allowed (digits, spaces, ``+ - ( ) .``), with at most
    one ``+`` and only at the start. After ``normalize_phone`` the number must be
    8-15 digits with a non-zero country code, and a number in the default country
    must have exactly the national length (10 digits for India).

    >>> is_valid_phone("+91 98765 43210"), is_valid_phone("12345")
    (True, False)
    """
    if not raw or not raw.strip():
        return False
    s = raw.strip()
    if not set(s) <= _TYPED_CHARS or s.count("+") > 1 or "+" in s[1:]:
        return False
    digits = normalize_phone(s, default_cc)
    if not (_E164_MIN <= len(digits) <= _E164_MAX) or digits.startswith("0"):
        return False
    if digits.startswith(default_cc):
        return len(digits) == len(default_cc) + _NATIONAL_LEN
    return True
