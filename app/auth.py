import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.config import get_settings

SESSION_KEY = "authenticated"


def verify_password(password: str) -> bool:
    expected = get_settings().cctv_password
    return secrets.compare_digest(password.encode(), expected.encode())


def login(request: Request) -> None:
    request.session[SESSION_KEY] = True


def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


def require_login(request: Request) -> None:
    if not request.session.get(SESSION_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )


LoginRequired = Annotated[None, Depends(require_login)]
