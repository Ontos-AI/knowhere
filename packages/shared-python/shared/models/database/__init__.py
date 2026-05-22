"""
Database Models Package
Ensure all models are correctly imported to avoid circular import issues
"""

# Import models in dependency order
# 1. First import base models (no foreign key dependencies)
from .user import User

# 2. Then import models that depend on User
from .api_key import APIKey
from .credits_transaction import CreditsTransaction
from .document import (
    Document,
    DocumentChunk,
    DocumentSection,
    GraphEdge,
    GraphNode,
    RetrievalHitStat,
    RetrievalRun,
    RetrievalStep,
)
from .document_page_plan import DocumentPagePlan
from .demo_materialization import DemoMaterialization
from .guest_device import GuestDevice
from .job import Job
from .job_result import JobChunk, JobResult
from .parse_agent import ParseRun, ParseStep

# 3. Job-related log models
from .job_state_audit_log import JobStateAuditLog
from .job_state_history import JobStateHistory
from .payment_record import PaymentRecord
from .stripe_price_config import StripePriceConfig
from .system_limit import SystemLimit

# 4. Rate limit configuration models
from .tier_limit import TierLimit
from .user_balance import UserBalance
from .webhook import WebhookEvent, WebhookEventStatus
from .webhook_log import WebhookLog
from .webhook_secret import WebhookSecret

# 5. Finally import other models
# from .oauth_provider import OAuthProvider  # Commented out temporarily to avoid circular imports

__all__ = [
    "User",
    "APIKey",
    "GuestDevice",
    "CreditsTransaction",
    "UserBalance",
    "Job",
    "JobResult",
    "JobChunk",
    "Document",
    "DocumentSection",
    "DocumentChunk",
    "DocumentPagePlan",
    "DemoMaterialization",
    "GraphNode",
    "GraphEdge",
    "RetrievalHitStat",
    "RetrievalRun",
    "RetrievalStep",
    "ParseRun",
    "ParseStep",
    "StripePriceConfig",
    "PaymentRecord",
    "JobStateAuditLog",
    "JobStateHistory",
    "WebhookEvent",
    "WebhookEventStatus",
    "WebhookLog",
    "WebhookSecret",
    "TierLimit",
    "SystemLimit",
    # "OAuthProvider"
]
