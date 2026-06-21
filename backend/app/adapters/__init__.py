"""Source adapters for Finnish job listing sources."""

from app.adapters.base import NormalizedListing, SourceAdapter
from app.adapters.duunitori import DuunitoriAdapter
from app.adapters.eures import EuresAdapter
from app.adapters.jobly import JoblyAdapter
from app.adapters.kirkkorekry import KirkkorekryAdapter
from app.adapters.kuntarekry import KuntarekryAdapter
from app.adapters.laura import LauraAdapter
from app.adapters.tmt import TmtAdapter
from app.adapters.tmt_oulu import TmtOuluAdapter
from app.adapters.varbi import OuluVarbiAdapter

__all__ = [
    "DuunitoriAdapter",
    "EuresAdapter",
    "JoblyAdapter",
    "KirkkorekryAdapter",
    "KuntarekryAdapter",
    "LauraAdapter",
    "NormalizedListing",
    "OuluVarbiAdapter",
    "SourceAdapter",
    "TmtAdapter",
    "TmtOuluAdapter",
]
