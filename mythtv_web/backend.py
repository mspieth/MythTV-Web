"""Validation shared by MythTV service responses."""


def json_object(response, field: str | None = None) -> dict:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("MythTV returned a non-object response")
    if field is not None:
        payload = payload.get(field)
        if not isinstance(payload, dict):
            raise ValueError(f"MythTV returned an invalid {field}")
    return payload


def program_list(response) -> dict:
    payload = json_object(response, "ProgramList")
    programs = payload.get("Programs", [])
    if not isinstance(programs, list) or any(
        not isinstance(program, dict) for program in programs
    ):
        raise ValueError("MythTV returned an invalid program list")
    for key in ("StartIndex", "Count", "TotalAvailable"):
        value = payload.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError(f"MythTV returned an invalid {key}")
    return payload


def cuttings(response) -> list:
    result = json_object(response, "CutList").get("Cuttings", [])
    if not isinstance(result, list):
        raise ValueError("MythTV returned an invalid cut list")
    return result
