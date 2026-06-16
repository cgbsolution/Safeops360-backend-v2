"""Schemas for the consolidated /api/dashboard/overview endpoint that powers
the mobile (and eventually the web) EHS dashboard. One round-trip returns
every KPI, trend, pyramid, top-unsafe and recent-activity slice the dashboard
needs — server-side aggregation keeps the mobile bundle thin and avoids
shipping thousands of raw rows over the wire."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DashboardKpis(BaseModel):
    daysSinceLastLti: int
    ltifr12mo: float
    trir12mo: float
    activePermits: int
    observationsMtd: int
    observationsOpen: int
    observationsClosed: int
    nearMiss12mo: int
    trainingCompliancePct: int
    inspectionCompliancePct: int


class TrendPoint(BaseModel):
    month: str
    observations: int
    nearMiss: int


class HeinrichLevel(BaseModel):
    level: str
    count: int
    color: str


class TopUnsafeCategory(BaseModel):
    category: str
    count: int


class RecentActivityItem(BaseModel):
    type: str
    title: str
    meta: str
    date: datetime
    tone: str
    # Deep-link info — the mobile client uses these to navigate straight to
    # the record's detail screen when the user taps the row.
    recordId: str | None = None
    module: str | None = None


class DashboardOverview(BaseModel):
    asOf: datetime
    kpis: DashboardKpis
    trend6mo: list[TrendPoint]
    heinrich: list[HeinrichLevel]
    topUnsafe: list[TopUnsafeCategory]
    recentActivity: list[RecentActivityItem]
