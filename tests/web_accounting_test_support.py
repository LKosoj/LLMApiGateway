from __future__ import annotations

import secrets
from contextlib import ExitStack
from unittest.mock import create_autospec, patch

from llm_gateway_core.api.v1 import web
from llm_gateway_core.services.accounting import AccountingReservation
from llm_gateway_core.services.accounting_service import AccountingService


def install_web_accounting_passthrough(stack: ExitStack) -> None:
    """Keep legacy web tests focused on their existing HTTP contracts."""
    owner = object()
    observation = object()
    accounting_service = create_autospec(
        AccountingService,
        instance=True,
        spec_set=True,
    )

    async def reserve(**kwargs: object) -> AccountingReservation:
        return AccountingReservation(
            reservation_id=secrets.token_hex(16),
            request_id=str(kwargs["request_id"]),
            api_key_id=kwargs.get("api_key_id"),  # type: ignore[arg-type]
            reserved_usd=float(kwargs["estimate_usd"]),
        )

    accounting_service.reserve.side_effect = reserve
    accounting_service.release.return_value = True

    async def finalize(_owner, response, _observation):
        return response

    async def release(_owner, *, primary_error=None) -> None:
        return None

    stack.enter_context(patch("main.AccountingService", return_value=accounting_service))
    stack.enter_context(
        patch.object(web, "take_operation_terminal_owner", return_value=owner)
    )
    stack.enter_context(
        patch.object(web, "_parse_web_terminal_observation", return_value=observation)
    )
    stack.enter_context(
        patch.object(web, "finalize_buffered_operation", side_effect=finalize)
    )
    stack.enter_context(
        patch.object(web, "release_operation_if_open", side_effect=release)
    )
