import re

from flask import Blueprint, g, redirect, render_template, request, session, url_for

from ..models import User
from ..utils.session_auth import login_required, login_user, logout_user, safe_next_url


site_bp = Blueprint("site", __name__)


@site_bp.route("/")
@login_required()
def home():
    date_str = (request.args.get("date") or "").strip()
    if date_str and re.match(r"^\\d{4}-\\d{2}-\\d{2}$", date_str):
        # Backward-compatible: whiteboard used to live on home with ?date=...
        return redirect(url_for("site.whiteboard", date=date_str))
    return render_template("index.html", page="home", title="Benoss")


@site_bp.route("/blog")
@login_required()
def blog():
    return render_template("blog.html", page="blog", title="Blog")


@site_bp.route("/note")
@login_required()
def note():
    return render_template("note.html", page="note", title="Note")


@site_bp.route("/echoes")
@login_required()
def echoes():
    return render_template("echoes.html", page="echoes", title="Echoes")


@site_bp.route("/dailyreal")
@login_required()
def dailyreal():
    return render_template("dailyreal.html", page="dailyreal", title="Dailyreal")


@site_bp.route("/whiteboard")
@login_required()
def whiteboard():
    return render_template("whiteboard.html", page="whiteboard", title="Whiteboard")


@site_bp.route("/control-room")
@login_required()
def control_room():
    return render_template("control_room.html", page="control-room", title="Control Room")


@site_bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = safe_next_url(request.args.get("next", ""))
    error = None
    remember_default = True
    if g.get("user"):
        return redirect(next_url or url_for("site.home"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "on"
        remember_default = remember
        if not username or not password:
            error = "Missing credentials"
        else:
            user = User.query.filter_by(username=username, is_active=True).first()
            if user and user.check_password(password):
                login_user(user)
                session.permanent = remember
                return redirect(next_url or url_for("site.home"))
            error = "Invalid username or password"
    return render_template(
        "login.html",
        page="login",
        title="Login",
        error=error,
        next_url=next_url,
        remember_default=remember_default,
    )


@site_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("site.login"))
