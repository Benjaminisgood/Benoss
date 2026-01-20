from flask import Blueprint, g, redirect, render_template, request, url_for

from ..models import User
from ..utils.session_auth import login_required, login_user, logout_user, safe_next_url


site_bp = Blueprint("site", __name__)


@site_bp.route("/")
@login_required()
def home():
    return render_template("index.html", page="home", title="Benoss")


@site_bp.route("/blog")
@login_required()
def blog():
    return render_template("blog.html", page="blog", title="Blog")


@site_bp.route("/note")
@login_required()
def note():
    return render_template("note.html", page="note", title="Note")


@site_bp.route("/everyday")
@login_required()
def everyday():
    return render_template("everyday.html", page="everyday", title="Asset Library")


@site_bp.route("/dailyreel")
@login_required()
def dailyreel():
    user = g.get("user")
    if user and user.role == "admin":
        return redirect(url_for("site.dailyreel_manage"))
    return redirect(url_for("site.dailyreel_view"))


@site_bp.route("/dailyreel/view")
@login_required()
def dailyreel_view():
    return render_template("dailyreel_view.html", page="dailyreel-view", title="Daily Reel")


@site_bp.route("/dailyreel/manage")
@login_required(role="admin")
def dailyreel_manage():
    return render_template("dailyreel_manage.html", page="dailyreel-manage", title="Daily Reel Studio")


@site_bp.route("/control-room")
@login_required()
def control_room():
    return render_template("control_room.html", page="control-room", title="Control Room")


@site_bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = safe_next_url(request.args.get("next", ""))
    error = None
    if g.get("user"):
        return redirect(next_url or url_for("site.home"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "Missing credentials"
        else:
            user = User.query.filter_by(username=username, is_active=True).first()
            if user and user.check_password(password):
                login_user(user)
                return redirect(next_url or url_for("site.home"))
            error = "Invalid username or password"
    return render_template("login.html", page="login", title="Login", error=error, next_url=next_url)


@site_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("site.login"))
