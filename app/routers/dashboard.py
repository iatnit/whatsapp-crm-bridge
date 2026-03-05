"""Customer analytics dashboard routes."""

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.auth import verify_admin

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
async def dashboard():
    """Customer analytics dashboard."""
    html = _load_dashboard_html()
    return HTMLResponse(html)


@router.get("/api/v1/dashboard/data", dependencies=[Depends(verify_admin)])
async def dashboard_data():
    """Return aggregated CRM stats for the dashboard."""
    from app.store.conversations import get_overview_stats
    return await get_overview_stats()


_dashboard_html_cache: str | None = None


def _load_dashboard_html() -> str:
    global _dashboard_html_cache
    if _dashboard_html_cache is None:
        path = Path(__file__).parent.parent / "static" / "dashboard.html"
        if path.exists():
            _dashboard_html_cache = path.read_text()
        else:
            # Fallback: inline minimal HTML (in case static file not created yet)
            _dashboard_html_cache = "<html><body><h1>Dashboard — static file missing</h1></body></html>"
    return _dashboard_html_cache
