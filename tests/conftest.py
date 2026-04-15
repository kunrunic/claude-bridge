# tests/conftest.py
"""
P4-2: 테스트 간 모듈 격리 — bot 모듈 캐시를 각 테스트 후 제거
"""
import sys
import pytest


@pytest.fixture(autouse=True)
def clean_bot_module():
    """각 테스트 전/후로 bot 모듈 캐시 격리"""
    yield
    sys.modules.pop("bot", None)
