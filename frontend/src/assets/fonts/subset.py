"""Subset the three UI fonts to Latin + Cyrillic + the symbols BridgeBox uses.

Dev-time only. Run once when a font is added or updated; the .woff2 outputs
are committed. See frontend/src/assets/fonts/README.md.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "out"
OUT.mkdir(parents=True, exist_ok=True)

UNICODES = ",".join(
    [
        "U+0000-00FF",  # Basic Latin + Latin-1 Supplement (includes · used in log origins)
        "U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC",
        "U+0400-045F,U+0490-0491,U+04B0-04B1",  # Cyrillic (the whole UI is Russian)
        "U+2000-206F",  # General punctuation: — – " " ' ' … •
        "U+20AC,U+2074,U+2116",  # €, superscript 4, № (Russian number sign)
        "U+2122,U+2212,U+2215",
        "U+2190-2193",  # ← ↑ → ↓  (the logs "↓ Новые записи" control)
        "U+25CF,U+2713-2717",  # ● and ✓ ✔ ✕ ✖ ✗ (status dots, diagnostic badges)
        "U+FEFF,U+FFFD",
    ]
)

# tnum matters more than usual here: log timestamps and per-strategy millisecond
# timings sit in columns, and proportional digits make them jitter.
FEATURES = "kern,liga,calt,tnum,ccmp,mark,mkmk"

FONTS = {
    "InterVariable.ttf": "Inter-subset.woff2",
    "Manrope.ttf": "Manrope-subset.woff2",
    "JetBrainsMono.ttf": "JetBrainsMono-subset.woff2",
}

for source, target in FONTS.items():
    src, dst = HERE / source, OUT / target
    subprocess.run(
        [
            sys.executable, "-m", "fontTools.subset", str(src),
            f"--output-file={dst}",
            "--flavor=woff2",
            f"--unicodes={UNICODES}",
            f"--layout-features={FEATURES}",
            "--desubroutinize",
            "--name-IDs=*",          # keep the family/licence names inside the file
            "--drop-tables+=DSIG",
        ],
        check=True,
    )
    print(f"{target:32} {src.stat().st_size / 1024:7.0f} KB -> {dst.stat().st_size / 1024:6.1f} KB")
