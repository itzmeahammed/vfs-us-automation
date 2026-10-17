"""A portable, read-only analytics assistant for a local dashboard.

Copy this folder into a project, register read-only tools that return the
project's data, and mount it on the project's HTTP server. See README.md.

Standard library only — no third-party dependencies.
"""
from .config import AssistantConfig
from .engine import Assistant
from .llm import ChatClient, LLMError
from .redact import Redactor
from .sqlite_tool import ReadOnlySQLite, register_sqlite_tool
from .tools import Tool, ToolError, ToolRegistry
from .web import AssistantHTTP

__all__ = [
    "Assistant", "AssistantConfig", "AssistantHTTP", "ChatClient", "LLMError",
    "ReadOnlySQLite", "Redactor", "Tool", "ToolError", "ToolRegistry",
    "register_sqlite_tool",
]
__version__ = "1.0.0"
