"""Web layer: server-rendered dashboard, case pages and the JSON API (BUILD_SPEC 4.3; concept 9).

Standard library only. :mod:`pms.web.app` provides ``serve()`` (``python -m pms serve``) and the WSGI
callable ``application`` (``gunicorn pms.web.app:application``). Every permission decision is made by
the domain (:class:`pms.workflow.Workflow`); the web layer only identifies the caller (:mod:`pms.auth`).
"""
