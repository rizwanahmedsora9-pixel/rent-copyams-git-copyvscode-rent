"""Package: the ledger module (journal, chart of accounts, periods, statements)."""
from ._common import *  # noqa: F401,F403
from .pages import *  # noqa: F401,F403
from .periods_pages import *  # noqa: F401,F403

from utils.pkg_wire import wire_package

wire_package("blueprints.ledger")
