"""The dashboard's six sections, one module each, under /api/dash.

1. analytics     -- the funnel, all-time and today
2. monitoring    -- the communities we check, and their leads
3. leads         -- every lead, split by what Vini said
4. communities   -- the whole communities table
5. attention     -- what needs a person; activity -- the log
6. config        -- schedules, lead rules, and how the services are doing
"""

from __future__ import annotations

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from circle_leads.web.sections import (
        activity, analytics, attention, communities, config, leads, monitoring,
    )

    for module in (analytics, monitoring, leads, communities, attention, activity, config):
        app.include_router(module.router)
