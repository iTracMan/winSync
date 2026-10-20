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
