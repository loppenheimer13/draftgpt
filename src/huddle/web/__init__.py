"""The parent-facing web app.

The FastAPI instance is deliberately *not* re-exported here: binding the name
``app`` in the package would shadow the ``huddle.web.app`` module, so
``from huddle.web import app`` would hand you the instance while
``import huddle.web.app`` handed you the module. Import it explicitly:

    from huddle.web.app import app
"""
