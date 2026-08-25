"""Shared domain errors used across plugin layers."""

from __future__ import annotations


class ShinjukuError(Exception):
    """Expected domain error that can be safely presented to users."""

    def __init__(self, message: str, code: str = "SHINJUKU_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code
