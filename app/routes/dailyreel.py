import calendar
from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import Project, ProjectActivity, User
from ..utils.session_auth import login_required


dailyreel_bp = Blueprint("dailyreel", __name__)


def _validate_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("invalid date") from exc
    return date_str


def _validate_month(month_str: str) -> str:
    try:
        datetime.strptime(month_str, "%Y-%m")
    except ValueError as exc:
        raise ValueError("invalid month") from exc
    return month_str


def _tz_offset_delta(value: str | None) -> timedelta:
    try:
        minutes = int(value or 0)
    except ValueError:
        minutes = 0
    minutes = max(-14 * 60, min(14 * 60, minutes))
    return timedelta(minutes=minutes)


@dailyreel_bp.route("/api/dailyreel/today", methods=["GET"])
@login_required()
def dailyreel_today():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    offset = _tz_offset_delta(request.args.get("tz_offset"))
    local_start = datetime.strptime(date_str, "%Y-%m-%d")
    start = local_start + offset
    end = start + timedelta(days=1)

    users = User.query.filter_by(is_active=True).order_by(User.username.asc()).all()
    counts = {
        user.id: {
            "user_id": user.id,
            "username": user.username,
            "git_blog": 0,
            "git_note": 0,
            "clone": 0,
        }
        for user in users
    }

    # Events (git/clone) for the day.
    query = (
        db.session.query(ProjectActivity, Project, User)
        .join(Project, ProjectActivity.project_id == Project.id)
        .join(User, ProjectActivity.actor_user_id == User.id)
        .filter(
            ProjectActivity.created_at >= start,
            ProjectActivity.created_at < end,
            ProjectActivity.type.in_(["git", "clone"]),
        )
        .order_by(ProjectActivity.created_at.desc(), ProjectActivity.id.desc())
        .limit(1000)
    )

    events = []
    for act, project, actor in query.all():
        row = counts.get(actor.id)
        if row is not None:
            if act.type == "git":
                if act.module == "blog":
                    row["git_blog"] += 1
                elif act.module == "note":
                    row["git_note"] += 1
            elif act.type == "clone":
                row["clone"] += 1

        events.append(
            {
                "id": act.id,
                "type": act.type,
                "module": act.module,
                "created_at": act.created_at.isoformat() + "Z" if act.created_at else None,
                "actor": {"id": actor.id, "username": actor.username},
                "project": {
                    "id": project.id,
                    "title": project.title,
                    "module": project.module,
                    "owner_id": project.owner_id,
                },
            }
        )

    # Sort the scoreboard by total effort, then name.
    scoreboard = list(counts.values())
    scoreboard.sort(key=lambda r: (-(r["git_blog"] + r["git_note"] + r["clone"]), r["username"].lower()))

    return jsonify(
        {
            "date": date_str,
            "scoreboard": scoreboard,
            "events": events,
        }
    )


@dailyreel_bp.route("/api/dailyreel/month", methods=["GET"])
@login_required()
def dailyreel_month():
    user = g.get("user")
    if user is None:
        return jsonify({"error": "login required"}), 401

    month_str = (request.args.get("month") or "").strip()
    if not month_str:
        month_str = datetime.utcnow().strftime("%Y-%m")
    try:
        month_str = _validate_month(month_str)
    except ValueError:
        return jsonify({"error": "invalid month"}), 400

    offset = _tz_offset_delta(request.args.get("tz_offset"))

    local_month_start = datetime.strptime(month_str, "%Y-%m").replace(day=1)
    year = local_month_start.year
    month = local_month_start.month
    num_days = calendar.monthrange(year, month)[1]

    if month == 12:
        local_month_end = local_month_start.replace(year=year + 1, month=1, day=1)
    else:
        local_month_end = local_month_start.replace(month=month + 1, day=1)

    start = local_month_start + offset
    end = local_month_end + offset

    acts = (
        ProjectActivity.query.filter(
            ProjectActivity.actor_user_id == user.id,
            ProjectActivity.created_at >= start,
            ProjectActivity.created_at < end,
            ProjectActivity.type.in_(["git", "clone"]),
        )
        .order_by(ProjectActivity.created_at.asc(), ProjectActivity.id.asc())
        .all()
    )

    by_date = {}
    for act in acts:
        if not act.created_at:
            continue
        local_dt = act.created_at - offset
        date_key = local_dt.strftime("%Y-%m-%d")
        row = by_date.get(date_key)
        if row is None:
            row = {"git_blog": 0, "git_note": 0, "clone": 0}
            by_date[date_key] = row
        if act.type == "git":
            if act.module == "blog":
                row["git_blog"] += 1
            elif act.module == "note":
                row["git_note"] += 1
        elif act.type == "clone":
            row["clone"] += 1

    days = []
    for day in range(1, num_days + 1):
        date_key = f"{year:04d}-{month:02d}-{day:02d}"
        row = by_date.get(date_key) or {"git_blog": 0, "git_note": 0, "clone": 0}
        total = int(row["git_blog"] or 0) + int(row["git_note"] or 0) + int(row["clone"] or 0)
        days.append(
            {
                "date": date_key,
                "git_blog": int(row["git_blog"] or 0),
                "git_note": int(row["git_note"] or 0),
                "clone": int(row["clone"] or 0),
                "total": total,
            }
        )

    return jsonify({"month": month_str, "days": days})
