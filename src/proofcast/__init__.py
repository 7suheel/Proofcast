"""Proofcast: an honest, multi-agent market analyst with a public, tamper-evident track record.

Not investment advice. Forecasts can be wrong. Past performance does not predict future results.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("proofcast")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0+unknown"

DISCLAIMER = (
    "Not investment advice. Forecasts can be wrong. "
    "Past performance does not predict future results."
)

__all__ = ["DISCLAIMER", "__version__"]
