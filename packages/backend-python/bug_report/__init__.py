"""Bug-report backend for Python apps.

Wire it into any Flask app:

    from flask import Flask
    from bug_report import create_blueprint, FilesystemStore

    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        store=FilesystemStore("/var/lib/bugs"),
        is_admin=lambda req: req.headers.get("X-Admin") == "yes",  # your auth
    ), url_prefix="/api")

``PostgresStore`` (``bug_report.pg``) is an optional extra and is not imported
here: ``pip install "bug-report-py[postgres]"``, then
``from bug_report.pg import PostgresStore``.

Implements the contract in ../spec/openapi.yaml. Cross-stack contract tests
in ../../e2e enforce that this and the Node backend stay honest.
"""

from .blueprint import create_blueprint
from .importer import import_reports
from .proxy import create_proxy_blueprint
from .trust import Caller, TrustedCallers, trusted_caller
from . import identity
from .email import AcsNotifier, Notice, render
from .store import (
    Store,
    StoreError,
    NotFound,
    InMemoryStore,
    FilesystemStore,
    BugRecord,
    AuditEntry,
)
from .models import (
    BUG_DETAILS_MAX,
    BUG_SCREENSHOT_MAX,
    BUG_STATUSES,
    new_bug_id,
    person_name,
    first_name,
    valid_bug_id,
)

__version__ = "3.0.0"

from . import contract
from .contract import (
    KINDS,
    TRANSITIONS,
    OFFERED,
    REASON_REQUIRED_FOR,
    CLOSED_STATUSES,
    kind_from_tags,
    is_valid_transition,
    is_closed,
    with_kind,
    effective_kind,
    PRODUCTS,
    APP_PRODUCTS,
    PRIORITIES,
    TRIAGE_FIELDS,
    product_for_app,
)

__all__ = [
    "create_blueprint",
    "create_proxy_blueprint",
    "import_reports",
    "trusted_caller",
    "TrustedCallers",
    "Caller",
    "identity",
    "AcsNotifier",
    "Notice",
    "render",
    "contract",
    "KINDS",
    "TRANSITIONS",
    "OFFERED",
    "REASON_REQUIRED_FOR",
    "CLOSED_STATUSES",
    "kind_from_tags",
    "is_valid_transition",
    "is_closed",
    "with_kind",
    "Store",
    "StoreError",
    "NotFound",
    "InMemoryStore",
    "FilesystemStore",
    "BugRecord",
    "AuditEntry",
    "BUG_DETAILS_MAX",
    "BUG_SCREENSHOT_MAX",
    "BUG_STATUSES",
    "new_bug_id",
    "valid_bug_id",
    "person_name",
    "first_name",
]
