"""Import every model here so `Base.metadata` is fully populated for Alembic
autogenerate and for `Base.metadata.create_all()` in tests."""

from app.infrastructure.database.models.business_hours import (
    HoursExceptionModel,
    WeeklyHoursModel,
)
from app.infrastructure.database.models.business_profile import BusinessProfileModel
from app.infrastructure.database.models.emergency_keyword import EmergencyKeywordModel
from app.infrastructure.database.models.faq_entry import FAQEntryModel
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.refresh_token import RefreshTokenModel
from app.infrastructure.database.models.role import (
    PermissionModel,
    RoleModel,
    role_permissions,
    user_roles,
)
from app.infrastructure.database.models.service import ServiceModel
from app.infrastructure.database.models.service_area import ServiceAreaModel
from app.infrastructure.database.models.user import UserModel

__all__ = [
    "BusinessProfileModel",
    "EmergencyKeywordModel",
    "FAQEntryModel",
    "HoursExceptionModel",
    "OrganizationModel",
    "PermissionModel",
    "RefreshTokenModel",
    "RoleModel",
    "ServiceAreaModel",
    "ServiceModel",
    "UserModel",
    "WeeklyHoursModel",
    "role_permissions",
    "user_roles",
]
