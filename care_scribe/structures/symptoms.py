from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from care.emr.resources.condition.spec import ClinicalStatusChoices, VerificationStatusChoices, SeverityChoices
from care_scribe.structures.utils import Code, StructuredQuestion, get_code


class Onset(BaseModel):
    onset_datetime: datetime


class Symptom(BaseModel):
    code: Code
    clinical_status: ClinicalStatusChoices
    verification_status: VerificationStatusChoices
    severity: SeverityChoices
    onset: Onset = Field(default_factory=lambda: Onset(onset_datetime=datetime.now()))
    recorded_date: Optional[datetime] = None
    note: Optional[str] = None


class SymptomToolCall(BaseModel):
    symptom: str
    clinical_status: ClinicalStatusChoices
    verification_status: VerificationStatusChoices
    severity: SeverityChoices
    onset_datetime: datetime
    recorded_date: Optional[datetime] = None
    note: Optional[str] = None


class SymptomsStructuredQuestion(StructuredQuestion):
    name = "Symptoms"
    key = "symptoms"

    class Structure(BaseModel):
        __root__: List[Symptom]

    class ToolStructure(BaseModel):
        __root__: List[SymptomToolCall]

    def deserialize(self, data: list[dict]) -> Structure:
        symptoms = []

        for item in data:
            symptom_query = item.get("symptom")
            code = get_code(symptom_query, type="system-condition-code")
            if not code:
                # skip if code is not found
                continue
            symptom = Symptom(
                code=code,
                clinical_status=item.get("clinical_status"),
                verification_status=item.get("verification_status"),
                severity=item.get("severity"),
                onset=Onset(onset_datetime=item.get("onset_datetime", datetime.now())),
                recorded_date=item.get("recorded_date"),
                note=item.get("note"),
            )
            symptoms.append(symptom)

        return self.Structure(__root__=symptoms)
