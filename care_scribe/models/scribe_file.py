import enum
import care.facility.FileUpload

class ScribeFile(care.facility.FileUpload):
    class FileType(enum.Enum):
        PATIENT = 1
        CONSULTATION = 2
        SAMPLE_MANAGEMENT = 3
        CLAIM = 4
        DISCHARGE_SUMMARY = 5
        COMMUNICATION = 6
        CONSENT_RECORD = 7
        SCRIBE = 8