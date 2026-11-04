"""UI-free service functions behind the management HTTP API (see docs/plans/2026-11-04-management-api-design.md).

One function, two callers: the API router and (from phase 3) the NiceGUI page both call these, so
the API can never disagree with the UI. Functions return plain dicts and raise ``errors.ServiceError``
subclasses; nothing here imports NiceGUI or FastAPI.
"""
