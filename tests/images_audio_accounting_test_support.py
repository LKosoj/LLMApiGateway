from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import create_autospec, patch

from llm_gateway_core.api.v1 import audio, images
from llm_gateway_core.services.accounting import AccountingReservation
from llm_gateway_core.services.accounting_service import AccountingService


def install_images_audio_accounting_passthrough(stack: ExitStack) -> None:
    """Keep compatibility tests focused on image/audio HTTP behavior."""
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

    async def finalize(_owner, response, _observation):
        return response

    async def release(_owner, *, primary_error=None) -> None:
        return None

    stack.enter_context(patch("main.AccountingService", return_value=accounting_service))
    for module, parser_name in (
        (images, "_parse_image_terminal_observation"),
        (audio, "_parse_audio_terminal_observation"),
    ):
        stack.enter_context(
            patch.object(module, "take_operation_terminal_owner", return_value=owner)
        )
        stack.enter_context(patch.object(module, parser_name, return_value=observation))
        stack.enter_context(
            patch.object(module, "finalize_buffered_operation", side_effect=finalize)
        )
        stack.enter_context(
            patch.object(module, "release_operation_if_open", side_effect=release)
        )
