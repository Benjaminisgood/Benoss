from datetime import datetime
from typing import Dict

from ..oss import get_object_json, put_object_json
from ..utils.oss_paths import month_map_key


def _parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def _month_str(date_str: str) -> str:
    dt = _parse_date(date_str)
    return dt.strftime("%Y-%m")


def _default_month_map(month: str) -> Dict:
    return {
        "month": month,
        "days": {},
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


def load_month_map(month: str) -> Dict:
    key = month_map_key(month)
    data = get_object_json(key)
    if data is None:
        return _default_month_map(month)
    return data


def save_month_map(month: str, data: Dict) -> None:
    data["updated_at"] = datetime.utcnow().isoformat() + "Z"
    key = month_map_key(month)
    put_object_json(key, data)


def get_day_entry(date_str: str) -> Dict:
    month = _month_str(date_str)
    data = load_month_map(month)
    return data["days"].get(date_str, {"text": "", "attachments": []})


def upsert_day_text(date_str: str, text: str) -> Dict:
    month = _month_str(date_str)
    data = load_month_map(month)
    day = data["days"].get(date_str, {"text": "", "attachments": []})
    day["text"] = text
    data["days"][date_str] = day
    save_month_map(month, data)
    return day


def add_attachment(date_str: str, attachment: Dict) -> Dict:
    month = _month_str(date_str)
    data = load_month_map(month)
    day = data["days"].get(date_str, {"text": "", "attachments": []})
    day["attachments"].append(attachment)
    data["days"][date_str] = day
    save_month_map(month, data)
    return day


def list_month(date_str_or_month: str) -> Dict:
    month = date_str_or_month
    if len(date_str_or_month) == 10:
        month = _month_str(date_str_or_month)
    return load_month_map(month)
