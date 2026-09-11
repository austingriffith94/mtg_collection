"""
Shared library code for the Streamlit dashboard (dashboard.py + pages/*.py).

Kept deliberately Streamlit-light where possible:
  - formatting.py and queries.py have NO Streamlit dependency, so they can be
    unit-tested (or reused by a future print-to-PDF script) without a running
    Streamlit app.
  - db.py and card_view.py hold the Streamlit-specific caching / widgets.
"""
