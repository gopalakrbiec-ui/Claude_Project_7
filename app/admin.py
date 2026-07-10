from __future__ import annotations

"""
Admin dashboard — read-mostly browsable UI over the core tables, mounted at
/admin. Built with SQLAdmin (auto-generates list/search/filter/detail views
from the SQLAlchemy models already used everywhere else in the app).

Auth: single shared username/password (ADMIN_USERNAME / ADMIN_PASSWORD env
vars), session-cookie based. The dashboard is entirely disabled if
ADMIN_PASSWORD is unset — safe default, no accidental exposure.

Scope note: tool-job history (AI Filter, Outfit, Remix, video tools, etc.)
lives in Redis, not Postgres — this dashboard shows GenerationJob rows
(the template-order pipeline) as the "API calls" proxy. Ad-hoc tool calls
aren't queryable here; see GET /tools/jobs for that data per-user.
"""

from fastapi import FastAPI
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.core.config import get_settings
from app.models.agent import AgentProfile
from app.models.generation_job import GenerationJob
from app.models.inspire_gallery import InspireGalleryItem
from app.models.ledger import CreditLedger
from app.models.order import Order
from app.models.payment import Payment
from app.models.template import Template
from app.models.user import User


class AdminAuth(AuthenticationBackend):
    """Single shared-credential login, backed by the signed session cookie."""

    async def login(self, request: Request) -> bool:
        form = await request.form()
        settings = get_settings()
        username = form.get("username", "")
        password = form.get("password", "")
        if not settings.admin_password:
            return False
        if username == settings.admin_username and password == settings.admin_password:
            request.session["admin_authenticated"] = True
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool | RedirectResponse:
        return bool(request.session.get("admin_authenticated"))


class UserAdmin(ModelView, model=User):
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-user"
    column_list = [User.id, User.name, User.phone, User.email, User.city, User.role, User.created_at]
    column_searchable_list = [User.name, User.phone, User.email]
    column_sortable_list = [User.id, User.created_at, User.role]
    column_default_sort = [(User.created_at, True)]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 50


class OrderAdmin(ModelView, model=Order):
    name = "Order"
    name_plural = "Orders (Template Photos)"
    icon = "fa-solid fa-image"
    column_list = [
        Order.id, Order.user_id, Order.template_id, Order.status,
        Order.price_paise, Order.agent_id, Order.created_at,
    ]
    column_searchable_list = [Order.idempotency_key]
    column_sortable_list = [Order.id, Order.created_at, Order.status, Order.price_paise]
    column_default_sort = [(Order.created_at, True)]
    column_filters = [Order.status, Order.user_id, Order.template_id]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 50


class GenerationJobAdmin(ModelView, model=GenerationJob):
    name = "Generation Job"
    name_plural = "Generation Jobs (API calls)"
    icon = "fa-solid fa-microchip"
    column_list = [
        GenerationJob.id, GenerationJob.order_id, GenerationJob.status,
        GenerationJob.provider, GenerationJob.cost_paise, GenerationJob.error, GenerationJob.created_at,
    ]
    column_sortable_list = [GenerationJob.id, GenerationJob.created_at, GenerationJob.status]
    column_default_sort = [(GenerationJob.created_at, True)]
    column_filters = [GenerationJob.status, GenerationJob.provider]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 50


class PaymentAdmin(ModelView, model=Payment):
    name = "Payment"
    name_plural = "Payments"
    icon = "fa-solid fa-indian-rupee-sign"
    column_list = [
        Payment.id, Payment.user_id, Payment.gateway, Payment.status,
        Payment.amount_paise, Payment.gateway_payment_id, Payment.created_at,
    ]
    column_searchable_list = [Payment.gateway_order_id, Payment.gateway_payment_id]
    column_sortable_list = [Payment.id, Payment.created_at, Payment.amount_paise, Payment.status]
    column_default_sort = [(Payment.created_at, True)]
    column_filters = [Payment.status, Payment.gateway, Payment.user_id]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 50


class CreditLedgerAdmin(ModelView, model=CreditLedger):
    name = "Ledger Entry"
    name_plural = "Wallet Ledger (Balance = Sum per user)"
    icon = "fa-solid fa-wallet"
    column_list = [
        CreditLedger.id, CreditLedger.user_id, CreditLedger.delta_paise,
        CreditLedger.reason, CreditLedger.ref_type, CreditLedger.ref_id, CreditLedger.created_at,
    ]
    column_sortable_list = [CreditLedger.id, CreditLedger.created_at, CreditLedger.delta_paise]
    column_default_sort = [(CreditLedger.created_at, True)]
    column_filters = [CreditLedger.reason, CreditLedger.user_id]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 50


class AgentAdmin(ModelView, model=AgentProfile):
    name = "Agent"
    name_plural = "Agents"
    icon = "fa-solid fa-handshake"
    column_list = [
        AgentProfile.user_id, AgentProfile.status, AgentProfile.commission_rate_bps,
        AgentProfile.payout_upi_vpa,
    ]
    can_create = False
    can_delete = False
    page_size = 50


class TemplateAdmin(ModelView, model=Template):
    name = "Template"
    name_plural = "Templates"
    icon = "fa-solid fa-palette"
    column_list = [
        Template.id, Template.name, Template.category, Template.theme,
        Template.base_price_paise, Template.active, Template.is_featured,
    ]
    column_searchable_list = [Template.name, Template.category]
    column_filters = [Template.category, Template.active, Template.is_featured]
    can_create = False
    can_delete = False
    page_size = 50


class InspireGalleryAdmin(ModelView, model=InspireGalleryItem):
    name = "Inspire Gallery Item"
    name_plural = "Inspire Gallery (AI-generated)"
    icon = "fa-solid fa-images"
    column_list = [
        InspireGalleryItem.id, InspireGalleryItem.category,
        InspireGalleryItem.active, InspireGalleryItem.created_at,
    ]
    column_filters = [InspireGalleryItem.category, InspireGalleryItem.active]
    can_create = False
    page_size = 50


def setup_admin(app: FastAPI, engine) -> None:
    """Mounts /admin if ADMIN_PASSWORD is configured; no-op otherwise."""
    settings = get_settings()
    if not settings.admin_password:
        return

    admin = Admin(
        app,
        engine,
        authentication_backend=AdminAuth(secret_key=settings.app_secret_key),
        title="Savi Nenapu Admin",
    )
    admin.add_view(UserAdmin)
    admin.add_view(OrderAdmin)
    admin.add_view(GenerationJobAdmin)
    admin.add_view(PaymentAdmin)
    admin.add_view(CreditLedgerAdmin)
    admin.add_view(AgentAdmin)
    admin.add_view(TemplateAdmin)
    admin.add_view(InspireGalleryAdmin)
