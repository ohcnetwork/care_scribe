from abc import ABC, abstractmethod
from typing import Any
from pydantic import BaseModel


class StructuredQuestion(ABC):
    structure: BaseModel
    name: str
    key: str

    class Structure(BaseModel, ABC):
        pass

    class ToolStructure(BaseModel, ABC):
        pass

    @abstractmethod
    def deserialize(self, data: Any) -> Structure:
        pass


class Code(BaseModel):
    code: str
    display: str
    system: str


def get_code(query: str, type: str, system: str = "http://snomed.info/sct") -> Code:
    return Code(code=query, display=query, system=system)
