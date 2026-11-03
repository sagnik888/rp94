"""Vendored copy of the user's "S1 + S2 Dual Independent" Python scanner, version 1.2.0.

Source: C:/Users/sagnik/Downloads/S1_S2_DUAL_PYTHON_SCANNER_v1.2_SINGLE_STOP_PARITY_FIXED/s1s2_python_scanner
(package `scanner`, pyproject version 1.2.0), vendored 2026-10-10 for the s1s2_dual engine.
Never import the Downloads copy at runtime; engine/s12_engine.py is the only host adapter.

Original SHA256 (the package's MANIFEST_SHA256.json; all verified before copying):
    scanner/__init__.py         68e1016b5a66170d6944260a746d84eed8f1c1b108bf2c951138105139522964
    scanner/config.py           9f222124b017638d5bc5d8f686dbf79390786e5672c6a1d3a3ce9841f2faf6c2
    scanner/engine.py           563cd8f0a2caae37cd47498aca9b39f558e26a4855e422481fe4b77290746a59
    scanner/generated_rules.py  a629c3b9a2c989562d8bfcb3da6137bee0464e8173c89e1bfbe39a4d4b29ecec
    scanner/indicators.py       0f079d7e199de9b3ef52fb0ed579a88093c61dc9a633348fb03251d68283df35
    scanner/mtf.py              a9acb9103fe9002249fea95a46842c5f6e5ac2a5eca997f9ddc97d8ea5247585
    scanner/timing.py           495783d79cb19e932c55280d918cd038c43d5971feea69ab40962896c275f398
    config.defaults.json        55818502cc447f94fa256d6333ded479b5810d27d8ff95b93ed70f3a76d77772
scanner/cli.py (1572bb38...) is not vendored.

Local changes, nothing else differs from the source files:
    __init__.py  this header.
    engine.py    the S2 bar-close parity fix, plus the host's documented exit policy:
                 R-based runner stops after T1 and a weakness-confirmed -0.5R early stop.
                 SHA256 after removing only the clock parity patch: 1128d6c955ebd84a421f4fdcf7a74cfaa3273094fb64545ef53e2ff4b2637533.
"""
from .config import Config
from .engine import DualScanner
__all__=['Config','DualScanner']
