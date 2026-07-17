from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import create_autospec, patch

from llm_gateway_core.api.v1 import pdf
from llm_gateway_core.services.accounting import AccountingReservation
from llm_gateway_core.services.accounting_service import AccountingService


def install_pdf_accounting_passthrough(stack: ExitStack) -> None:
    """Keep compatibility tests focused on the established PDF HTTP contract."""
    owner = object()
    observation = object()
    accounting_service = create_autospec(
        AccountingService,
        instance=True,
        spec_set=True,
    )

    async def reserve(**kwargs) -> AccountingReservation:
        request_id = str(kwargs["request_id"])
        return AccountingReservation(
            reservation_id=f"test-reservation-{request_id}",
            request_id=request_id,
            api_key_id=kwargs.get("api_key_id"),
            reserved_usd=float(kwargs["estimate_usd"]),
        )

    accounting_service.reserve.side_effect = reserve
    accounting_service.release.return_value = True

    async def finalize(_owner, response, _observation, **_kwargs):
        return response

    async def finalize_job(_owner, response, _payload, **_kwargs):
        return response

    async def release(_owner, *, primary_error=None) -> None:
        return None

    stack.enter_context(patch("main.AccountingService", return_value=accounting_service))
    stack.enter_context(
        patch.object(pdf, "take_operation_terminal_owner", return_value=owner)
    )
    stack.enter_context(
        patch.object(pdf, "_parse_pdf_terminal_observation", return_value=observation)
    )
    stack.enter_context(
        patch.object(pdf, "finalize_buffered_operation", side_effect=finalize)
    )
    stack.enter_context(
        patch.object(pdf, "_finalize_pdf_job_response", side_effect=finalize_job)
    )
    stack.enter_context(
        patch.object(pdf, "release_operation_if_open", side_effect=release)
    )
