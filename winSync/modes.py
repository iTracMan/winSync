"""Mode tables: Thetis DSP mode numbers (as reported by ZZMD;/ZZME;/ZZIF;) -> KiwiSDR mode strings."""

# Thetis DSPMode -> two-digit CAT number, from Mode2String() in Thetis CATCommands.cs
THETIS_LABELS = {
    0: "LSB",
    1: "USB",
    2: "DSB",
    3: "CWL",
    4: "CWU",
    5: "FM",
    6: "AM",
    7: "DIGU",
    8: "SPEC",
    9: "DIGL",
    10: "SAM",
    11: "DRM",
}

# Default Thetis mode -> Kiwi mode. Kiwi has no DSB or reverse CW, and DRM needs a Kiwi extension,
# so those map to the closest plain mode. SPEC (Thetis's panadapter-only mode) has no equivalent
# and is not applied. Overridable via [modes] in config.toml.
DEFAULT_THETIS_TO_KIWI = {
    0: "lsb",
    1: "usb",
    2: "am",
    3: "cw",
    4: "cw",
    5: "nbfm",
    6: "am",
    7: "usb",
    9: "lsb",
    10: "sam",
    11: "am",
}

# Mode strings the Kiwi client understands (passbands_fallback in kiwisdr.min.js).
# Anything else silently becomes "am" inside Kiwi, so we validate before sending.
KIWI_MODES = frozenset(
    "am amn amw sam sal sau sas qam drm lsb lsn usb usn cw cwn nbfm nnfm iq".split()
)


def thetis_to_kiwi(mode, overrides=None):
    """Return the Kiwi mode string for a Thetis mode number, or None if unmapped/invalid."""
    table = dict(DEFAULT_THETIS_TO_KIWI)
    if overrides:
        table.update({int(k): v for k, v in overrides.items()})
    kiwi = table.get(mode)
    return kiwi if kiwi in KIWI_MODES else None


# Kiwi -> radio (two-way sync). Each Kiwi mode -> the plain mode it is a variant of.
KIWI_FAMILY = {
    "am": "am", "amn": "am", "amw": "am",
    "sam": "sam", "sal": "sam", "sau": "sam", "sas": "sam",
    "lsb": "lsb", "lsn": "lsb", "usb": "usb", "usn": "usb",
    "cw": "cw", "cwn": "cw", "nbfm": "nbfm", "nnfm": "nbfm",
}

# Plain Kiwi mode -> Thetis mode number. CW is CWU unless the radio is already in CWL.
# Only these can ever be set on the radio (see SET_RE in cat.py).
FAMILY_TO_THETIS = {"lsb": 0, "usb": 1, "cw": 4, "nbfm": 5, "am": 6, "sam": 10}


def kiwi_to_thetis(kiwi_mode, current=None, overrides=None):
    """Thetis mode number to set for a Kiwi mode, or None to leave the radio's mode alone: when the
    radio's mode already maps to the same kind of Kiwi mode (DIGU and usb, CWL and cw, ...), or the
    Kiwi mode has no radio equivalent (iq, drm, qam)."""
    family = KIWI_FAMILY.get(kiwi_mode)
    if family is None:
        return None
    if current is not None and KIWI_FAMILY.get(thetis_to_kiwi(current, overrides)) == family:
        return None
    return FAMILY_TO_THETIS[family]
