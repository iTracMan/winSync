"""Frequency -> amateur band name."""

# (name, low_hz, high_hz) inclusive; generous edges so all regions are covered.
BANDS = [
    ("2200m", 135_700, 137_800),
    ("630m", 472_000, 479_000),
    ("160m", 1_800_000, 2_000_000),
    ("80m", 3_500_000, 4_000_000),
    ("60m", 5_250_000, 5_450_000),
    ("40m", 7_000_000, 7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m", 50_000_000, 54_000_000),
    ("4m", 70_000_000, 70_500_000),
    ("2m", 144_000_000, 148_000_000),
]


def band_for(freq_hz):
    """Return the band name containing freq_hz, or 'GEN' outside the amateur bands."""
    for name, lo, hi in BANDS:
        if lo <= freq_hz <= hi:
            return name
    return "GEN"
