from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class CustomerCallerIdentityModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Which telephony lines a customer has called from.

    Deliberately a separate table rather than a `caller_number` column on
    `customers`. A column would create a *second* unique key on that table
    alongside `uq_customers_org_phone`, so a caller ID matching customer X
    while the stated callback number matched customer Y would have no
    defined resolution — and it could not represent a customer who calls
    from both a mobile and a landline.

    The unique constraint covers the whole triple, not
    `(organization_id, caller_number)`. A shared household or office line
    genuinely belongs to several customers, and a two-column unique
    constraint would silently award it to whoever was recorded first,
    causing the assistant to greet the wrong person by name. Ambiguity is
    represented honestly in the data and resolved by refusing to ground
    (see `CallerIdentityRepository.find_customers_by_caller_number`).
    """

    __tablename__ = "customer_caller_identities"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "caller_number",
            "customer_id",
            name="uq_caller_identity_org_number_customer",
        ),
        # The lookup path: org + number, every voice turn. Non-unique by
        # design, per the constraint reasoning above.
        Index("ix_caller_identities_org_number", "organization_id", "caller_number"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        # CASCADE, not SET NULL: an association to a deleted customer has
        # no meaning and would otherwise linger as an unresolvable row.
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # E.164 as supplied by the telephony provider. Same width as
    # `voice_calls.caller_number`, which is where the value comes from —
    # both sides of the comparison are provider-generated, which is why no
    # normalisation is needed here.
    caller_number: Mapped[str] = mapped_column(String(32), nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
