"""Turning what a customer typed into a number WhatsApp can reach.

People write their number the way they say it: 0712 345 678, (071) 234-5678,
+254 712 345 678. WhatsApp needs one form. Shopify normalises the phone a
customer enters at the checkout, but a number typed into an address is kept as
typed, and the country on that address is what makes it unambiguous. Nothing
here guesses.
"""

from __future__ import annotations

import re

# WhatsApp msisdns are 7 to 15 digits. E.164 caps the whole number at 15,
# country code included. The same limits Njiwa applies, repeated here so the
# settings page refuses a number for the same reason the API would.
MIN_MSISDN_DIGITS = 7
MAX_MSISDN_DIGITS = 15

_NOT_DIGITS = re.compile(r"\D")

# ISO 3166 country code to calling code. WooCommerce asks WooCommerce for
# this; Shopify has no such table to ask, so it ships here. It is the full
# list rather than the countries anybody expects, because a shortlist quietly
# mis-reads a number from a country nobody thought of. Only ever consulted to
# complete a local number; an international one is never checked against it.
CALLING_CODES: dict[str, str] = {
    "AD": "376", "AE": "971", "AF": "93", "AG": "1268", "AI": "1264", "AL": "355",
    "AM": "374", "AO": "244", "AR": "54", "AS": "1684", "AT": "43", "AU": "61",
    "AW": "297", "AX": "358", "AZ": "994", "BA": "387", "BB": "1246", "BD": "880",
    "BE": "32", "BF": "226", "BG": "359", "BH": "973", "BI": "257", "BJ": "229",
    "BL": "590", "BM": "1441", "BN": "673", "BO": "591", "BQ": "599", "BR": "55",
    "BS": "1242", "BT": "975", "BW": "267", "BY": "375", "BZ": "501", "CA": "1",
    "CC": "61", "CD": "243", "CF": "236", "CG": "242", "CH": "41", "CI": "225",
    "CK": "682", "CL": "56", "CM": "237", "CN": "86", "CO": "57", "CR": "506",
    "CU": "53", "CV": "238", "CW": "599", "CX": "61", "CY": "357", "CZ": "420",
    "DE": "49", "DJ": "253", "DK": "45", "DM": "1767", "DO": "1809", "DZ": "213",
    "EC": "593", "EE": "372", "EG": "20", "EH": "212", "ER": "291", "ES": "34",
    "ET": "251", "FI": "358", "FJ": "679", "FK": "500", "FM": "691", "FO": "298",
    "FR": "33", "GA": "241", "GB": "44", "GD": "1473", "GE": "995", "GF": "594",
    "GG": "44", "GH": "233", "GI": "350", "GL": "299", "GM": "220", "GN": "224",
    "GP": "590", "GQ": "240", "GR": "30", "GT": "502", "GU": "1671", "GW": "245",
    "GY": "592", "HK": "852", "HN": "504", "HR": "385", "HT": "509", "HU": "36",
    "ID": "62", "IE": "353", "IL": "972", "IM": "44", "IN": "91", "IO": "246",
    "IQ": "964", "IR": "98", "IS": "354", "IT": "39", "JE": "44", "JM": "1876",
    "JO": "962", "JP": "81", "KE": "254", "KG": "996", "KH": "855", "KI": "686",
    "KM": "269", "KN": "1869", "KP": "850", "KR": "82", "KW": "965", "KY": "1345",
    "KZ": "7", "LA": "856", "LB": "961", "LC": "1758", "LI": "423", "LK": "94",
    "LR": "231", "LS": "266", "LT": "370", "LU": "352", "LV": "371", "LY": "218",
    "MA": "212", "MC": "377", "MD": "373", "ME": "382", "MF": "590", "MG": "261",
    "MH": "692", "MK": "389", "ML": "223", "MM": "95", "MN": "976", "MO": "853",
    "MP": "1670", "MQ": "596", "MR": "222", "MS": "1664", "MT": "356", "MU": "230",
    "MV": "960", "MW": "265", "MX": "52", "MY": "60", "MZ": "258", "NA": "264",
    "NC": "687", "NE": "227", "NF": "672", "NG": "234", "NI": "505", "NL": "31",
    "NO": "47", "NP": "977", "NR": "674", "NU": "683", "NZ": "64", "OM": "968",
    "PA": "507", "PE": "51", "PF": "689", "PG": "675", "PH": "63", "PK": "92",
    "PL": "48", "PM": "508", "PR": "1787", "PS": "970", "PT": "351", "PW": "680",
    "PY": "595", "QA": "974", "RE": "262", "RO": "40", "RS": "381", "RU": "7",
    "RW": "250", "SA": "966", "SB": "677", "SC": "248", "SD": "249", "SE": "46",
    "SG": "65", "SH": "290", "SI": "386", "SJ": "47", "SK": "421", "SL": "232",
    "SM": "378", "SN": "221", "SO": "252", "SR": "597", "SS": "211", "ST": "239",
    "SV": "503", "SX": "1721", "SY": "963", "SZ": "268", "TC": "1649", "TD": "235",
    "TG": "228", "TH": "66", "TJ": "992", "TK": "690", "TL": "670", "TM": "993",
    "TN": "216", "TO": "676", "TR": "90", "TT": "1868", "TV": "688", "TW": "886",
    "TZ": "255", "UA": "380", "UG": "256", "US": "1", "UY": "598", "UZ": "998",
    "VA": "39", "VC": "1784", "VE": "58", "VG": "1284", "VI": "1340", "VN": "84",
    "VU": "678", "WF": "681", "WS": "685", "XK": "383", "YE": "967", "YT": "262",
    "ZA": "27", "ZM": "260", "ZW": "263",
}


def to_msisdn(phone: str | None, country: str | None = "") -> str:
    """Digits only, in full international form, or "" if there is nothing usable.

    `phone` is as the customer typed it. `country` is the ISO code from the
    same address, such as KE.
    """
    raw = (phone or "").strip()
    if "@" in raw:
        # A WhatsApp address rather than a phone number, and one ending @g.us
        # is a group. Stripping it down to its digits would turn a group id
        # into something that looks like a number, so it is refused whole.
        return ""
    digits = _NOT_DIGITS.sub("", raw)
    if not digits:
        return ""

    # A leading + or 00 is the customer saying "this is the whole number".
    # Believe them, and stop before the country on the address gets a say:
    # somebody living abroad who buys with a card billed at home would
    # otherwise have their own country code treated as a local number and a
    # second one stuck in front of it.
    already_international = raw.startswith("+") or digits.startswith("00")

    # 00 is how much of the world dials out.
    if digits.startswith("00"):
        digits = digits[2:]

    if already_international:
        return _bounded(digits)

    code = CALLING_CODES.get((country or "").strip().upper(), "")
    if not code:
        # No country to reason with. Send it as written and let Njiwa resolve
        # it against the sending number's own country.
        return _bounded(digits)

    # Already international. The length test is what stops a national number
    # that happens to open with its own country's digits being mistaken for
    # one, which is a real hazard in +1 countries.
    if digits.startswith(code) and len(digits) >= len(code) + 7:
        return _bounded(digits)

    # The trunk prefix: the 0 you dial at home and never abroad.
    national = digits.lstrip("0")
    # Check the length BEFORE the code goes on. Afterwards is too late: 12345
    # is a typo, but 254 + 12345 is eight digits and passes the bounds, so a
    # mistyped number would be sent to whoever owns 25412345.
    if len(national) < MIN_MSISDN_DIGITS:
        return ""
    return _bounded(code + national)


def _bounded(digits: str) -> str:
    """Too few digits is a typo, too many is not a phone number. Either way,
    nothing is sent rather than a message to a stranger."""
    if MIN_MSISDN_DIGITS <= len(digits) <= MAX_MSISDN_DIGITS:
        return digits
    return ""


def parse_list(raw: str | None) -> list[str]:
    """A list typed by the shop owner: comma, semicolon or newline separated.

    Not space. People write a number with spaces in it - +254 700 000 003 -
    and every settings page in this package tells them to separate with
    commas, so a space is part of a number rather than the end of one.

    Digits and nothing else, as many of them as an msisdn has. "Contains a
    digit" would not do: 120363028712345678@g.us contains plenty, and Njiwa
    reads an address ending @g.us as a group without looking at the rest, so
    one new order would post to a WhatsApp group of hundreds from the shop's
    own number. A piece with an @ in it is dropped whole, not cleaned up.
    """
    found: list[str] = []
    for piece in (part.strip() for part in re.split(r"[,;\n]+", raw or "")):
        if not piece or "@" in piece:
            continue
        digits = _NOT_DIGITS.sub("", piece)
        if MIN_MSISDN_DIGITS <= len(digits) <= MAX_MSISDN_DIGITS and digits not in found:
            found.append(digits)
    return found


def rejected_from_list(raw: str | None) -> list[str]:
    """The pieces parse_list threw away, so the settings page can say which."""
    bad: list[str] = []
    for piece in (part.strip() for part in re.split(r"[,;\n]+", raw or "")):
        if not piece:
            continue
        digits = _NOT_DIGITS.sub("", piece)
        if "@" in piece or not (MIN_MSISDN_DIGITS <= len(digits) <= MAX_MSISDN_DIGITS):
            bad.append(piece)
    return bad
