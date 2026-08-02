from pydantic import BaseModel


class ActivityTotals(BaseModel):
    requests: int
    tokens: int
    # None when there's no data in the window to compute a rate from (e.g.
    # zero requests), rather than a misleading 0%/0s.
    success_rate: float | None
    avg_latency_seconds: float | None


class TimeSeriesPoint(BaseModel):
    t: int
    v: float


class ModelTimeSeries(BaseModel):
    model: str
    points: list[TimeSeriesPoint]


class TopKeyItem(BaseModel):
    key_id: str
    tokens: float


class TopModelItem(BaseModel):
    model: str
    requests: float


class ActivityOverviewResponse(BaseModel):
    range: str
    totals: ActivityTotals
    requests_by_model: list[ModelTimeSeries]
    top_keys_by_tokens: list[TopKeyItem]
    top_models_by_requests: list[TopModelItem]
