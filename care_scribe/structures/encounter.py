from typing import Optional
from pydantic import BaseModel, Field
from care.emr.resources.encounter.constants import (
    EncounterPriorityChoices,
    AdmitSourcesChoices,
    DischargeDispositionChoices,
    StatusChoices,
    ClassChoices,
    DietPreferenceChoices,
)
from care_scribe.structures.utils import StructuredQuestion


class Hospitalization(BaseModel):
    re_admission: bool
    admit_source: AdmitSourcesChoices
    diet_preference: Optional[DietPreferenceChoices] = None
    discharge_disposition: Optional[DischargeDispositionChoices]


class EncounterStructuredQuestion(StructuredQuestion):
    name = "Encounter"
    key = "encounter"
    description = "Captures details about the patient's encounter, including status, class, priority, and hospitalization details."

    class Structure(BaseModel):
        status: StatusChoices
        encounter_class: ClassChoices
        priority: EncounterPriorityChoices
        external_identifier: Optional[str]
        hospitalization: Optional[Hospitalization] = Field(
            None,
            description='Applicable if encounter_class is "imp", "obsenc", or "emer"',
        )

    class ToolStructure(BaseModel):
        encounter_status: StatusChoices
        encounter_class: ClassChoices = Field(
            ...,
            description=(
                'Class of the encounter: "imp" (Inpatient) | "amb" (Outpatient) | '
                '"obsenc" (Observation Room) | "emer" (Emergency) | "vr" (Virtual) | "hh" (Home Health)'
            ),
        )
        external_identifier: Optional[str] = Field(None, description="ip/op/obs/emr number")
        encounter_priority: EncounterPriorityChoices
        re_admission: bool = Field(
            ...,
            description="Whether the encounter is a re-admission. (Applicable if encounter_class is 'imp', 'obsenc', or 'emer')",
        )
        admit_source: AdmitSourcesChoices = Field(
            ...,
            description=(
                '(Applicable if encounter_class is "imp", "obsenc", or "emer")'
                'Admission source: "hosp_trans" (Hospital Transfer) | "emd" (Emergency Department) | '
                '"outp" (Outpatient Department) | "born" (Born) | "gp" (General Practitioner) | '
                '"mp" (Medical Practitioner) | "nursing" (Nursing Home) | '
                '"psych" (Psychiatric Hospital) | "rehab" (Rehabilitation Facility) | "other" (Other)'
            ),
        )
        diet_preference: Optional[DietPreferenceChoices] = Field(
            None, description=('Applicable if encounter_class is "imp", "obsenc", or "emer"')
        )
        discharge_disposition: Optional[DischargeDispositionChoices] = Field(
            None,
            description=(
                'Only applicable if status is "completed" and if encounter_class is "imp", "obsenc", or "emer"'
            ),
        )

    @staticmethod
    def deserialize(data: dict) -> Structure:
        encounter_class = data.get("encounter_class")
        hospitalization = (
            Hospitalization(
                re_admission=data.get("re_admission", False),
                admit_source=data.get("admit_source"),
                diet_preference=data.get("diet_preference"),
                discharge_disposition=data.get("discharge_disposition"),
            )
            if data and encounter_class in ["imp", "obsenc", "emer"]
            else None
        )

        return EncounterStructuredQuestion.Structure(
            status=data.get("encounter_status"),
            encounter_class=encounter_class,
            priority=data.get("encounter_priority"),
            external_identifier=data.get("external_identifier"),
            hospitalization=hospitalization,
        )
