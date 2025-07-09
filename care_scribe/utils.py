from typing import List, Union, Dict, Any
import copy

Field = Dict[str, Any]
Questionnaire = Dict[str, Any]


def count_fields(item: Union[Field, Questionnaire]) -> int:
    if "fields" not in item:
        return 1
    return sum(count_fields(f) for f in item["fields"])


def split_fields(fields: List[Union[Field, Questionnaire]], max_fields: int) -> List[List[Union[Field, Questionnaire]]]:
    chunks = []
    current_chunk = []
    current_count = 0

    def _add_chunk():
        nonlocal current_chunk, current_count
        if current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_count = 0

    for field in fields:
        if "fields" not in field:
            if current_count + 1 > max_fields:
                _add_chunk()
            current_chunk.append(field)
            current_count += 1
        else:
            # Nested questionnaire - split its fields recursively
            subchunks = split_fields(field["fields"], max_fields)
            for sub in subchunks:
                if current_count + len(sub) > max_fields:
                    _add_chunk()
                nested_copy = copy.deepcopy(field)
                nested_copy["fields"] = sub
                current_chunk.append(nested_copy)
                current_count += len(sub)
                if current_count >= max_fields:
                    _add_chunk()
    _add_chunk()
    return chunks


def chunk_questionnaires(questionnaires: List[Questionnaire], max_fields: int = 15) -> List[Questionnaire]:
    all_chunks = []
    for q in questionnaires:
        chunks = split_fields(q["fields"], max_fields)
        for chunk in chunks:
            q_copy = [{"title": q["title"], "description": q["description"], "fields": chunk}]
            all_chunks.append(q_copy)
    return all_chunks
