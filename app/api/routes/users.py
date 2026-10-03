"""
Admin-only user management.

This is also the reference CRUD shape for the template: list with search and
pagination, read, update, delete — copy it for your own resources.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.api.deps import CurrentUser, SessionDep, require_role
from app.models.user import Role, User
from app.schemas.auth import MessageResponse
from app.schemas.user import PageMeta, UserAdminUpdate, UserCreate, UserPage, UserRead
from app.services import auth as auth_service

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


@router.get("", response_model=UserPage)
async def list_users(
    session: SessionDep,
    q: str | None = Query(default=None, description="Matches email or name"),
    role: Role | None = None,
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> UserPage:
    filters = []

    if q:
        pattern = f"%{q.lower()}%"
        filters.append(
            or_(func.lower(User.email).like(pattern), func.lower(User.full_name).like(pattern))
        )
    if role is not None:
        filters.append(User.role == role)
    if is_active is not None:
        filters.append(User.is_active == is_active)

    total = await session.scalar(select(func.count()).select_from(User).where(*filters)) or 0

    result = await session.execute(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return UserPage(
        items=[UserRead.model_validate(user) for user in result.scalars()],
        meta=PageMeta(total=total, page=page, page_size=page_size),
    )


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, session: SessionDep) -> UserRead:
    if await auth_service.get_user_by_email(session, payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    user = await auth_service.create_user(
        session,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        role=payload.role,
    )
    return UserRead.model_validate(user)


@router.get("/{user_id}", response_model=UserRead)
async def read_user(user_id: uuid.UUID, session: SessionDep) -> UserRead:
    user = await session.get(User, user_id)

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return UserRead.model_validate(user)


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: uuid.UUID,
    payload: UserAdminUpdate,
    session: SessionDep,
    current_user: CurrentUser,
) -> UserRead:
    user = await session.get(User, user_id)

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    updates = payload.model_dump(exclude_unset=True)

    # Guard rails against locking yourself out of your own workspace
    if user.id == current_user.id:
        if "role" in updates and updates["role"] != user.role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot change your own role",
            )
        if updates.get("is_active") is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot deactivate your own account",
            )

    # Only an owner may hand out or take away the owner role
    if (
        "role" in updates
        and Role.OWNER in (updates["role"], user.role)
        and current_user.role is not Role.OWNER
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can manage the owner role",
        )

    for field, value in updates.items():
        setattr(user, field, value)

    # Deactivating should also end their sessions
    if updates.get("is_active") is False:
        await auth_service.revoke_all_sessions(session, user.id)

    await session.flush()
    return UserRead.model_validate(user)


@router.delete("/{user_id}", response_model=MessageResponse)
async def delete_user(
    user_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> MessageResponse:
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account",
        )

    user = await session.get(User, user_id)

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.role is Role.OWNER and current_user.role is not Role.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can delete an owner",
        )

    await session.delete(user)
    return MessageResponse(message="User deleted")
