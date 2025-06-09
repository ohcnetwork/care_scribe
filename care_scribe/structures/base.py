from openai import BaseModel
from pydantic import Field


class StringQuestion(BaseModel):
    value: str


class IntegerQuestion(BaseModel):
    value: int


class BooleanQuestion(BaseModel):
    value: bool


class DateQuestion(BaseModel):
    value: str = Field(..., description="YYYY-MM-DD format")


class DateTimeQuestion(BaseModel):
    value: str = Field(..., description="YYYY-MM-DDTHH:mm format")


ARBITRARY_QUESTION_MAPPINGS = {
    "string": StringQuestion,
    "integer": IntegerQuestion,
    "boolean": BooleanQuestion,
    "date": DateQuestion,
    "dateTime": DateTimeQuestion,
}
