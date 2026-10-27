"""Normalisation of Cameroon mobile (business SIM) numbers.

Stored form: the 9-digit national number starting with 6, e.g. 699123456.
Accepted input: '+237 699 12 34 56', '00237699123456', '237-699-123-456',
'6 99 12 34 56'.
"""
import re

_STRIP = re.compile(r"[\s\-\.\(\)/]")
_VALID = re.compile(r"^6\d{8}$")


class SimNumberError(ValueError):
    pass


def normalize_sim(raw: str) -> str:
    s = _STRIP.sub("", str(raw or ""))
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("00237"):
        s = s[5:]
    elif s.startswith("237") and len(s) == 12:
        s = s[3:]
    if not _VALID.match(s):
        raise SimNumberError("Enter a 9-digit Cameroon mobile number starting with 6 (e.g. 699 12 34 56)")
    return s


def display_sim(s: str) -> str:
    if s and len(s) == 9:
        return f"{s[0:3]} {s[3:5]} {s[5:7]} {s[7:9]}"
    return s or ""
