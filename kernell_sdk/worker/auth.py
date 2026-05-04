import os
from fastapi import Header, HTTPException
import secrets
from .config import AUTH_TOKEN

async def verify_token(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid auth format")

    token = authorization.split(" ")[1]

    if not secrets.compare_digest(token, AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")
