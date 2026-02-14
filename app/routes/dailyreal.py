import calendar
import json
from collections import Counter
from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, request
from sqlalchemy import func

from ..extensions import db
from ..models import Project, ProjectActivity, User, WhiteboardCard, WhiteboardEvent
from ..utils.session_auth import login_required


dailyreal_bp = Blueprint("dailyreal", __name__)


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


def _day_window(date_str: str, offset: timedelta) -> tuple[datetime, datetime]:
    local_start = datetime.strptime(date_str, "%Y-%m-%d")
    start = local_start + offset
    end = start + timedelta(days=1)
    return start, end


def _entry_tags(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        tag = str(item or "").strip()
        if not tag:
            continue
        out.append(tag[:24])
        if len(out) >= 12:
            break
    return out


def _counter_payload(counter: Counter, limit: int = 8) -> list[dict]:
    rows = []
    for key, count in counter.most_common(limit):
        rows.append({"name": key, "count": int(count)})
    return rows


@dailyreal_bp.route("/api/dailyreal/today", methods=["GET"])
@login_required()
def dailyreal_today():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    offset = _tz_offset_delta(request.args.get("tz_offset"))
    start, end = _day_window(date_str, offset)

    users = User.query.filter_by(is_active=True).order_by(User.username.asc()).all()
    counts = {
        user.id: {
            "user_id": user.id,
            "username": user.username,
            "git_blog": 0,
            "git_note": 0,
            "push": 0,
            "clone": 0,
            "whiteboard": 0,
        }
        for user in users
    }

    # Scoreboard counts: git/push/clone.
    for actor_user_id, type_, module, n in (
        db.session.query(
            ProjectActivity.actor_user_id,
            ProjectActivity.type,
            ProjectActivity.module,
            func.count(ProjectActivity.id),
        )
        .filter(
            ProjectActivity.created_at >= start,
            ProjectActivity.created_at < end,
            ProjectActivity.type.in_(["git", "clone", "push"]),
        )
        .group_by(ProjectActivity.actor_user_id, ProjectActivity.type, ProjectActivity.module)
        .all()
    ):
        row = counts.get(actor_user_id)
        if row is None:
            continue
        if type_ == "git":
            if module == "blog":
                row["git_blog"] += int(n or 0)
            elif module == "note":
                row["git_note"] += int(n or 0)
        elif type_ == "clone":
            row["clone"] += int(n or 0)
        elif type_ == "push":
            row["push"] += int(n or 0)

    # Scoreboard counts: whiteboard events.
    # For updates, count distinct card ids to avoid inflating score while typing/dragging.
    for actor_user_id, distinct_cards in (
        db.session.query(
            WhiteboardEvent.actor_user_id,
            func.count(func.distinct(WhiteboardEvent.card_id)),
        )
        .filter(
            WhiteboardEvent.created_at >= start,
            WhiteboardEvent.created_at < end,
            WhiteboardEvent.event_type == "update",
            WhiteboardEvent.card_id > 0,
        )
        .group_by(WhiteboardEvent.actor_user_id)
        .all()
    ):
        row = counts.get(actor_user_id)
        if row is not None:
            row["whiteboard"] += int(distinct_cards or 0)

    for actor_user_id, event_type, n in (
        db.session.query(
            WhiteboardEvent.actor_user_id,
            WhiteboardEvent.event_type,
            func.count(WhiteboardEvent.id),
        )
        .filter(
            WhiteboardEvent.created_at >= start,
            WhiteboardEvent.created_at < end,
            WhiteboardEvent.event_type.in_(["create", "delete", "reset", "link_create", "link_delete"]),
        )
        .group_by(WhiteboardEvent.actor_user_id, WhiteboardEvent.event_type)
        .all()
    ):
        row = counts.get(actor_user_id)
        if row is not None:
            row["whiteboard"] += int(n or 0)

    # Day feed events: project activity + whiteboard activity (most recent first).
    raw_events: list[tuple[datetime, dict]] = []

    for act, project, actor in (
        db.session.query(ProjectActivity, Project, User)
        .join(Project, ProjectActivity.project_id == Project.id)
        .join(User, ProjectActivity.actor_user_id == User.id)
        .filter(
            ProjectActivity.created_at >= start,
            ProjectActivity.created_at < end,
            ProjectActivity.type.in_(["git", "clone", "push"]),
        )
        .order_by(ProjectActivity.created_at.desc(), ProjectActivity.id.desc())
        .limit(800)
        .all()
    ):
        if not act.created_at:
            continue
        raw_events.append(
            (
                act.created_at,
                {
                    "id": f"p{act.id}",
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
                },
            )
        )

    for ev, actor in (
        db.session.query(WhiteboardEvent, User)
        .join(User, WhiteboardEvent.actor_user_id == User.id)
        .filter(
            WhiteboardEvent.created_at >= start,
            WhiteboardEvent.created_at < end,
            WhiteboardEvent.event_type.in_(["create", "update", "delete", "reset", "link_create", "link_delete"]),
        )
        .order_by(WhiteboardEvent.created_at.desc(), WhiteboardEvent.id.desc())
        .limit(800)
        .all()
    ):
        if not ev.created_at:
            continue
        raw_events.append(
            (
                ev.created_at,
                {
                    "id": f"w{ev.id}",
                    "type": ev.event_type,
                    "module": "whiteboard",
                    "created_at": ev.created_at.isoformat() + "Z" if ev.created_at else None,
                    "actor": {"id": actor.id, "username": actor.username},
                    "whiteboard": {
                        "date": ev.board_date,
                        "card_id": int(ev.card_id) if int(ev.card_id or 0) > 0 else None,
                    },
                },
            )
        )

    raw_events.sort(key=lambda r: (r[0] or datetime.min), reverse=True)
    events = [item for _, item in raw_events[:1000]]

    # Sort the scoreboard by total effort, then name.
    scoreboard = list(counts.values())
    scoreboard.sort(
        key=lambda r: (
            -(
                int(r.get("git_blog") or 0)
                + int(r.get("git_note") or 0)
                + int(r.get("push") or 0)
                + int(r.get("clone") or 0)
                + int(r.get("whiteboard") or 0)
            ),
            r["username"].lower(),
        )
    )

    type_counter: Counter = Counter()
    mood_counter: Counter = Counter()
    tag_counter: Counter = Counter()
    cards_for_day = (
        WhiteboardCard.query.filter(WhiteboardCard.board_date == date_str)
        .order_by(WhiteboardCard.id.asc())
        .all()
    )
    for card in cards_for_day:
        entry_type = str(card.entry_type or "").strip().lower() or "note"
        type_counter[entry_type] += 1
        mood = str(card.entry_mood or "").strip()
        if mood:
            mood_counter[mood] += 1
        for tag in _entry_tags(card.entry_tags_json):
            tag_counter[tag] += 1

    return jsonify(
        {
            "date": date_str,
            "scoreboard": scoreboard,
            "events": events,
            "whiteboard_meta": {
                "cards": len(cards_for_day),
                "types": _counter_payload(type_counter, limit=10),
                "moods": _counter_payload(mood_counter, limit=10),
                "tags": _counter_payload(tag_counter, limit=20),
            },
        }
    )


@dailyreal_bp.route("/api/dailyreal/month", methods=["GET"])
@login_required()
def dailyreal_month():
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
            ProjectActivity.type.in_(["git", "clone", "push"]),
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
            row = {"git_blog": 0, "git_note": 0, "push": 0, "clone": 0, "whiteboard": 0}
            by_date[date_key] = row
        if act.type == "git":
            if act.module == "blog":
                row["git_blog"] += 1
            elif act.module == "note":
                row["git_note"] += 1
        elif act.type == "push":
            row["push"] += 1
        elif act.type == "clone":
            row["clone"] += 1

    wb_updates_by_date: dict[str, set[int]] = {}
    wb_events = (
        WhiteboardEvent.query.filter(
            WhiteboardEvent.actor_user_id == user.id,
            WhiteboardEvent.created_at >= start,
            WhiteboardEvent.created_at < end,
        )
        .order_by(WhiteboardEvent.created_at.asc(), WhiteboardEvent.id.asc())
        .all()
    )
    for ev in wb_events:
        if not ev.created_at:
            continue
        local_dt = ev.created_at - offset
        date_key = local_dt.strftime("%Y-%m-%d")
        row = by_date.get(date_key)
        if row is None:
            row = {"git_blog": 0, "git_note": 0, "push": 0, "clone": 0, "whiteboard": 0}
            by_date[date_key] = row
        if ev.event_type == "update" and int(ev.card_id or 0) > 0:
            wb_updates_by_date.setdefault(date_key, set()).add(int(ev.card_id))
        else:
            row["whiteboard"] += 1

    for date_key, ids in wb_updates_by_date.items():
        row = by_date.get(date_key)
        if row is None:
            row = {"git_blog": 0, "git_note": 0, "push": 0, "clone": 0, "whiteboard": 0}
            by_date[date_key] = row
        row["whiteboard"] += len(ids)

    days = []
    for day in range(1, num_days + 1):
        date_key = f"{year:04d}-{month:02d}-{day:02d}"
        row = by_date.get(date_key) or {"git_blog": 0, "git_note": 0, "push": 0, "clone": 0, "whiteboard": 0}
        total = (
            int(row["git_blog"] or 0)
            + int(row["git_note"] or 0)
            + int(row["push"] or 0)
            + int(row["clone"] or 0)
            + int(row["whiteboard"] or 0)
        )
        days.append(
            {
                "date": date_key,
                "git_blog": int(row["git_blog"] or 0),
                "git_note": int(row["git_note"] or 0),
                "push": int(row.get("push") or 0),
                "clone": int(row["clone"] or 0),
                "whiteboard": int(row.get("whiteboard") or 0),
                "total": total,
            }
        )

    return jsonify({"month": month_str, "days": days})


@dailyreal_bp.route("/api/dailyreal/summary", methods=["GET"])
@login_required()
def dailyreal_summary():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    offset = _tz_offset_delta(request.args.get("tz_offset"))
    start, end = _day_window(date_str, offset)

    scoreboard = {}
    user_ids: set[int] = set()
    totals = {"git_blog": 0, "git_note": 0, "push": 0, "clone": 0, "whiteboard": 0}

    project_rows = (
        db.session.query(
            ProjectActivity.actor_user_id,
            ProjectActivity.type,
            ProjectActivity.module,
            func.count(ProjectActivity.id),
        )
        .filter(
            ProjectActivity.created_at >= start,
            ProjectActivity.created_at < end,
            ProjectActivity.type.in_(["git", "clone", "push"]),
        )
        .group_by(ProjectActivity.actor_user_id, ProjectActivity.type, ProjectActivity.module)
        .all()
    )
    for actor_user_id, type_, module, n in project_rows:
        uid = int(actor_user_id or 0)
        if uid <= 0:
            continue
        user_ids.add(uid)
        row = scoreboard.setdefault(uid, 0)
        count = int(n or 0)
        scoreboard[uid] = int(row) + count
        if type_ == "git":
            if module == "blog":
                totals["git_blog"] += count
            elif module == "note":
                totals["git_note"] += count
        elif type_ == "clone":
            totals["clone"] += count
        elif type_ == "push":
            totals["push"] += count

    wb_rows = (
        db.session.query(
            WhiteboardEvent.actor_user_id,
            WhiteboardEvent.event_type,
            func.count(WhiteboardEvent.id),
        )
        .filter(
            WhiteboardEvent.created_at >= start,
            WhiteboardEvent.created_at < end,
            WhiteboardEvent.event_type.in_(["create", "update", "delete", "reset", "link_create", "link_delete"]),
        )
        .group_by(WhiteboardEvent.actor_user_id, WhiteboardEvent.event_type)
        .all()
    )
    for actor_user_id, _event_type, n in wb_rows:
        uid = int(actor_user_id or 0)
        if uid <= 0:
            continue
        user_ids.add(uid)
        count = int(n or 0)
        scoreboard[uid] = int(scoreboard.get(uid) or 0) + count
        totals["whiteboard"] += count

    type_counter: Counter = Counter()
    mood_counter: Counter = Counter()
    tag_counter: Counter = Counter()
    cards_for_day = (
        WhiteboardCard.query.filter(WhiteboardCard.board_date == date_str)
        .order_by(WhiteboardCard.id.asc())
        .all()
    )
    for card in cards_for_day:
        type_counter[str(card.entry_type or "").strip().lower() or "note"] += 1
        mood = str(card.entry_mood or "").strip()
        if mood:
            mood_counter[mood] += 1
        for tag in _entry_tags(card.entry_tags_json):
            tag_counter[tag] += 1

    users = {
        user.id: user.username
        for user in User.query.filter(User.id.in_(list(user_ids))).all()
        if user and user.id
    }
    top_user_id = None
    top_user_score = 0
    for uid, score in scoreboard.items():
        if score > top_user_score:
            top_user_id = uid
            top_user_score = int(score)

    lines = [f"{date_str} 今日总结"]
    lines.append(
        "项目活动："
        f"Blog git {totals['git_blog']}，Note git {totals['git_note']}，push {totals['push']}，clone {totals['clone']}"
    )
    lines.append(
        "白板记录："
        f"{len(cards_for_day)} 条卡片，事件 {totals['whiteboard']} 次"
    )
    if type_counter:
        parts = [f"{name} {count}" for name, count in type_counter.most_common(4)]
        lines.append(f"记录类型：{' / '.join(parts)}")
    if tag_counter:
        parts = [f"#{name}({count})" for name, count in tag_counter.most_common(5)]
        lines.append(f"高频标签：{' '.join(parts)}")
    if mood_counter:
        parts = [f"{name}({count})" for name, count in mood_counter.most_common(4)]
        lines.append(f"情绪分布：{' / '.join(parts)}")
    if top_user_id and top_user_id in users:
        lines.append(f"今日最活跃：@{users[top_user_id]}（{top_user_score}）")
    else:
        lines.append("今日最活跃：暂无")

    return jsonify(
        {
            "date": date_str,
            "summary": "\n".join(lines),
            "totals": totals,
            "whiteboard_meta": {
                "cards": len(cards_for_day),
                "types": _counter_payload(type_counter, limit=10),
                "moods": _counter_payload(mood_counter, limit=10),
                "tags": _counter_payload(tag_counter, limit=20),
            },
            "top_user": {
                "id": int(top_user_id or 0) if top_user_id else None,
                "username": users.get(top_user_id or 0, "") if top_user_id else "",
                "score": int(top_user_score or 0),
            },
        }
    )
