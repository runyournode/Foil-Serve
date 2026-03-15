from typing import Annotated

from fastapi import (
    Depends,
    HTTPException,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from settings import settings

# -----------------------------------
# Incoming API key verification     -
# -----------------------------------
# Use HTTPBearer for Authorization header
security = HTTPBearer(auto_error=False)


async def verify_api_key(
    auth: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> str:
    """
    Check if client has a proper API key.
    """

    if not settings.requires_auth:
        return "auth not required"

    client_key = auth.credentials if auth else None
    if client_key is None or client_key not in settings.app_api_keys:
        masked_key = f"{client_key[:3]}**[masked]**" if client_key else "None"
        raise HTTPException(
            status_code=403,
            detail=f"Unauthorized access attempt. Key provided: {masked_key}",
        )
    return client_key
