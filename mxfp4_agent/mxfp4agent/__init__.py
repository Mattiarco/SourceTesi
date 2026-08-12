"""mxfp4agent — generazione agentica di unità aritmetiche MXFP4 in Meta-HDL."""

__version__ = "0.1.0"

from .config import Config
from .workflow import RunResult, Workflow

__all__ = ["Config", "Workflow", "RunResult", "__version__"]
