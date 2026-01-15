import uuid


def new_uuid() -> str:
    return uuid.uuid4().hex
