"""Bug-report backend for Python apps.

Wire it into any Flask app:

    from flask import Flask
    from bug_report import create_blueprint, FilesystemStore

    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        store=FilesystemStore("/var/lib/bugs"),
        is_admin=lambda req: req.headers.get("X-Admin") == "yes",  # your auth
    ), url_prefix="/api")

Implements the contract in ../spec/openapi.yaml. Cross-stack contract tests
in ../../e2e enforce that this and the Node backend stay honest.
"""

from .blueprint import create_blueprint
from .email import AcsNotifier, Notice, render
from .store import (
    Store,
    StoreError,
    NotFound,
    InMemoryStore,
    FilesystemStore,
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

__version__ = "1.0.0"

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
)

__all__ = [
    "create_blueprint",
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
    "BUG_DETAILS_MAX",
    "BUG_SCREENSHOT_MAX",
    "BUG_STATUSES",
    "new_bug_id",
    "valid_bug_id",
    "person_name",
    "first_name",
]
