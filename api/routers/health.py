"""Health and database readiness check endpoint (`GET /health`)."""

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from api.dependencies import get_api_repository
from api.repository import APIRepository
from api.schemas.responses import HealthResponse

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        status.HTTP_200_OK: {"model": HealthResponse, "description": "API and MongoDB are healthy"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": HealthResponse,
            "description": "API is up, but MongoDB is unreachable",
        },
    },
)
def get_health(repo: APIRepository = Depends(get_api_repository)) -> JSONResponse:
    """Report FastAPI process status and MongoDB connectivity separately."""
    health = repo.get_health()
    http_status = (
        status.HTTP_200_OK
        if health.mongodb_status == "connected"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(
        status_code=http_status,
        content=health.model_dump(mode="json"),
    )
