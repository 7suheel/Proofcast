"""Adapters for free data sources.

Each adapter takes an :class:`~proofcast.net.HttpClient` (so tests can plug in recorded
fixtures through ``httpx.MockTransport``) and returns Proofcast models with publication times.
"""
