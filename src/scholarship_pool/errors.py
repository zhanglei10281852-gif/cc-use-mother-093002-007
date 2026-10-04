"""领域异常。"""


class DomainError(Exception):
    """违反名额治理规则时抛出。"""


class ReplayIntegrityError(Exception):
    """离线重放结果与事件日志记录不一致时抛出。"""
