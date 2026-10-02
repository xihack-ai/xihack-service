"""HireSpec Loop: import AI functions and typed contracts from this package."""

from .evaluation import verify_evaluation
from .llm import CallableJSONClient, JSONClient
from .service import HireSpecAI
from .workflow import LoopSession

__all__ = ["CallableJSONClient", "HireSpecAI", "JSONClient", "LoopSession", "verify_evaluation"]
