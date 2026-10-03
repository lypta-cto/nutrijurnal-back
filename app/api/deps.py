import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import decode_token
from app.models.user import Role, User
from app.services import auth as auth_service

bearer_scheme = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None:
        raise CREDENTIALS_ERROR

    try:
        payload = decode_token(credentials.credentials, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise CREDENTIALS_ERROR from exc

    user = await auth_service.get_user_by_id(session, user_id)

    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(minimum: Role):
    """
    Route guard for a minimum rank:

        @router.delete("/{id}", dependencies=[Depends(require_role(Role.ADMIN))])
    """

    async def dependency(user: CurrentUser) -> User:
        if not user.role.can(minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires the {minimum.value} role or higher",
            )
        return user

    return dependency


AdminUser = Annotated[User, Depends(require_role(Role.ADMIN))]
