from abc import ABC, abstractmethod

from typing import Optional
from pydantic import BaseModel, Field
from datetime import datetime

class AbstractEvent(ABC, BaseModel):

    @property
    @classmethod
    @abstractmethod
    def event_type(cls) -> str:
        pass

    @property
    @classmethod
    @abstractmethod
    def description(cls) -> str:
        pass

    summary: Optional[str]
    created_at: datetime = Field(default_factory=datetime.now)
    sender_id: str
    target_id: Optional[str]
