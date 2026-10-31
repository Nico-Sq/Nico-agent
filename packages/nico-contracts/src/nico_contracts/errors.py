"""Stable errors crossing package and adapter boundaries."""


class ModelError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ChannelAccessError(ValueError):
    pass


class ChannelDeliveryError(RuntimeError):
    def __init__(self, message_id: str, delivery_id: str):
        super().__init__("渠道投递失败，可以使用同一消息 ID 重试")
        self.message_id = message_id
        self.delivery_id = delivery_id
