"""Compatibility exports for the channel contracts, use case and console adapter."""

from nico_adapters.console import ConsoleChannel
from nico_application.channels import ChannelLedgerPort, ChannelRouter
from nico_contracts.channels import ChannelResult, TextChannel, TextMessage
from nico_contracts.errors import ChannelAccessError, ChannelDeliveryError

__all__ = [
    "ChannelAccessError", "ChannelDeliveryError", "ChannelLedgerPort", "ChannelResult",
    "ChannelRouter", "ConsoleChannel", "TextChannel", "TextMessage",
]
