import os
from contextlib import contextmanager
from contextvars import ContextVar
from app.services.llm_service import llm_service, LLMUsageInfo, TopicsResponse, StreamResult
from app.services.image_service import ImageService

_llm_override = ContextVar('llm_service_override', default=None)


@contextmanager
def using_llm_service(service):
    """Task-local injection: original workflows keep their existing providers."""
    token = _llm_override.set(service)
    try:
        yield service
    finally:
        _llm_override.reset(token)


def get_llm_service():
    overridden = _llm_override.get()
    if overridden is not None:
        return overridden
    if os.getenv('MOCK_MODE', 'false').lower() == 'true':
        from app.services.mock_service import MockLLMService
        return MockLLMService()
    return llm_service


def get_image_service():
    return ImageService()
