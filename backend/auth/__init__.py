"""
Auth package — JWT validation, user context, and provider token management.
FastAPI depends on get_current_user() for all protected routes.
"""
from .middleware import get_current_user, optional_user
from .context import UserContext

__all__ = ["get_current_user", "optional_user", "UserContext"]
